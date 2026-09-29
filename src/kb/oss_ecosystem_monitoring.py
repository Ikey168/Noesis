"""Monitor releases, yanks, deprecations, licence and dependency changes through ``platform.subscriptions`` (OS10).

A package monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`); there is no monitor table,
scheduler or delivery path of its own. It follows one of:

* ``package`` - an exact canonical coordinate;
* ``graph`` - a release and a depth: every package in its current
  declared-constraint graph (bounded like :mod:`src.kb.oss_ecosystem_graph`);
* ``organisation`` - an organisation-level publisher declaration.

Each evaluation runs at a committed watermark and lists one item per change a
registry stated: ``release_published``, ``release_yanked``,
``release_deprecated``, ``release_unpublished``, ``licence_changed``,
``dependency_added``, ``dependency_removed`` and ``repository_link_changed``.
Items cite the old and the new revision and the source; the subscription
store's snapshot diff delivers each at most once, re-acquiring unchanged data
adds nothing, and revisions that arrived late (older than what was already
current) never produce an event. Advisory events stay with
``create_vulnerability_monitor``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from typing import Any

from src.kb.oss_ecosystem_identity import OssIdentity, package_key
from src.kb.oss_ecosystem_records import READ_SCOPE, REGISTRY_SOURCES, coordinate
from src.kb.oss_ecosystem_store import OssEcosystemStore, OssStoreError, authorize
from src.kb.oss_ecosystem_versions import UnsupportedConstraint, sort_key
from src.kb.oss_spdx import comparison_key_of

CONTRACT = "noesis-oss-package-notification-v1"
WATCH_KINDS = ("package", "graph", "organisation")
EVENT_KINDS = (
    "release_published",
    "release_yanked",
    "release_deprecated",
    "release_unpublished",
    "licence_changed",
    "dependency_added",
    "dependency_removed",
    "repository_link_changed",
)
STATE_EVENTS = {
    "yanked": "release_yanked",
    "deprecated": "release_deprecated",
    "unpublished": "release_unpublished",
}


class OssPackageMonitor:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = OssEcosystemStore(conn, initialize=False, now=self.now)
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    # ------------------------------------------------------------ subscriptions

    def create(
        self,
        namespace: str,
        request_key: str,
        *,
        watch: str,
        key: str,
        principal_id: str,
        scopes: Iterable[str],
        version: str | None = None,
        depth: int = 2,
        delivery: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        key = str(key or "").strip()
        if watch not in WATCH_KINDS or not key:
            raise OssStoreError(
                "invalid_watch", f"watch one of {WATCH_KINDS} with a key"
            )
        if watch in {"package", "graph"}:
            if not key.startswith("pkg:"):
                raise OssStoreError(
                    "invalid_watch", "a package is followed by its canonical coordinate"
                )
            ecosystem, _, name = key[4:].partition(":")
            key = coordinate(ecosystem, name)
        if watch == "graph" and (not version or not 0 <= int(depth) <= 5):
            raise OssStoreError(
                "invalid_watch", "a graph root names a version and a depth of 0-5"
            )
        query = {
            "operation": "search",
            "kind": "oss-package-monitor",
            "watch": watch,
            "key": key,
        }
        if watch == "graph":
            query.update({"version": str(version), "depth": int(depth)})
        created = self.subscriptions.create(
            {
                "namespace": namespace,
                "domain": "oss-ecosystems",
                "query": query,
                "filters": {"watch": watch},
                "cadence": {"trigger": "watermark"},
                "delivery": delivery or {"kind": "poll"},
            },
            "oss-package-monitor:" + request_key,
            principal_id=principal_id,
            scopes=scopes,
        )
        return {
            **created,
            "refresh": "source-pack runs and the maintenance orchestrator commit the watermarks this "
            "monitor evaluates; there is no separate scheduler",
        }

    def _subscription(
        self, subscription_id: str, principal_id: str, scopes: set[str]
    ) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(
            subscription_id, principal_id=principal_id, scopes=scopes
        )
        if subscription["query"].get("kind") != "oss-package-monitor":
            raise OssStoreError(
                "monitor_not_found", "subscription is not an OSS package monitor"
            )
        return subscription

    # ------------------------------------------------------------ followed packages

    def _packages(self, namespace: str, query: dict[str, Any]) -> list[str]:
        if query["watch"] == "package":
            return [query["key"]]
        if query["watch"] == "graph":
            from src.kb.oss_ecosystem_graph import DependencyGraphs

            graph = DependencyGraphs(self.conn).graph(
                namespace,
                query["key"],
                query["version"],
                self.now(),
                scopes={"operator"},
                depth=query["depth"],
            )
            return sorted({n["coordinate"] for n in graph["nodes"]} | {query["key"]})
        from src.kb.oss_ecosystem_queries import OssQueries

        answer = OssQueries(self.conn).packages_by_organisation(
            namespace, query["key"], scopes={"operator"}
        )
        return sorted({p["package"] for p in answer["packages"]})

    # ------------------------------------------------------------ items

    def _chain(self, record_id: str) -> list[dict[str, Any]]:
        """Revisions that were current when they arrived; late history never produces an event."""

        return self.store.change_chain(record_id)

    @staticmethod
    def _item(
        kind: str,
        item_id: str,
        coord: str,
        source: str,
        new: dict[str, Any],
        old: dict[str, Any] | None,
        **extra: Any,
    ) -> dict[str, Any]:
        return {
            "id": item_id,
            "kind": kind,
            "package": coord,
            "source": source,
            "new_revision_id": new["revision_id"],
            "observed_at_ms": new["observed_at_ms"],
            **({"old_revision_id": old["revision_id"]} if old else {}),
            **extra,
        }

    def _release_items(
        self, namespace: str, coord: str, source: str
    ) -> list[dict[str, Any]]:
        items = []
        for record in self.store.records(
            namespace,
            record_type="release_state_revision",
            source=source,
            coordinate=coord,
        ):
            chain, previous = self._chain(record["record_id"]), None
            for revision in chain:
                state = revision["statement"]["state"]
                version = record["version"]
                if previous is None or previous["statement"]["state"] != state:
                    if state == "published" and previous is None:
                        items.append(
                            self._item(
                                "release_published",
                                f"{coord}@{version}:release_published",
                                coord,
                                source,
                                revision,
                                None,
                                version=version,
                                published_at=revision["statement"].get("published_at"),
                            )
                        )
                    elif state in STATE_EVENTS:
                        items.append(
                            self._item(
                                STATE_EVENTS[state],
                                f"{coord}@{version}:{STATE_EVENTS[state]}:{revision['revision_id']}",
                                coord,
                                source,
                                revision,
                                previous,
                                version=version,
                                reason=revision["statement"].get("reason"),
                            )
                        )
                previous = revision
        return items

    def _ordered(
        self, namespace: str, record_type: str, coord: str, source: str
    ) -> list[tuple[str, dict]]:
        eco = coord.split(":")[1]
        rows = []
        for record in self.store.records(
            namespace, record_type=record_type, source=source, coordinate=coord
        ):
            chain = self._chain(record["record_id"])
            if not chain:
                continue
            try:
                key = (0, sort_key(eco, record["version"]))
            except UnsupportedConstraint:
                key = (1, record["version"])
            rows.append((key, record["version"], chain))
        rows.sort(key=lambda r: r[0])
        return [(version, chain) for _, version, chain in rows]

    def _licence_items(
        self, namespace: str, coord: str, source: str
    ) -> list[dict[str, Any]]:
        versions = self.store.spdx_versions(namespace)
        pinned = versions[-1] if versions else None
        items, previous = [], None

        def key(revision: dict[str, Any]) -> str:
            return comparison_key_of(
                self.store.normalisation(revision["revision_id"], pinned)
            ) or json.dumps(revision["statement"]["raw"], sort_keys=True)

        for version, chain in self._ordered(
            namespace, "licence_declaration_revision", coord, source
        ):
            for earlier, later in zip(
                chain, chain[1:]
            ):  # a correction of one release's declaration
                if key(earlier) != key(later):
                    items.append(
                        self._item(
                            "licence_changed",
                            f"{coord}@{version}:licence_changed:{later['revision_id']}",
                            coord,
                            source,
                            later,
                            earlier,
                            version=version,
                            change="correction of a release's declaration",
                        )
                    )
            current = chain[-1]
            if previous is not None and key(previous[1]) != key(current):
                items.append(
                    self._item(
                        "licence_changed",
                        f"{coord}:licence_changed:{previous[0]}->{version}:"
                        f"{current['revision_id']}",
                        coord,
                        source,
                        current,
                        previous[1],
                        from_version=previous[0],
                        version=version,
                        spdx_list_version=pinned,
                    )
                )
            previous = (version, current)
        return items

    def _dependency_items(
        self, namespace: str, coord: str, source: str
    ) -> list[dict[str, Any]]:
        items, previous = [], None

        def entries(revision: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
            return {
                (e["name"], e["scope"]): e for e in revision["statement"]["entries"]
            }

        for version, chain in self._ordered(
            namespace, "declared_dependency_set", coord, source
        ):
            current = chain[-1]
            if previous is not None:
                before, after = entries(previous[1]), entries(current)
                for name, scope in sorted(set(after) - set(before)):
                    items.append(
                        self._item(
                            "dependency_added",
                            f"{coord}:dependency_added:{previous[0]}->{version}:"
                            f"{name}:{scope}",
                            coord,
                            source,
                            current,
                            previous[1],
                            from_version=previous[0],
                            version=version,
                            dependency=name,
                            scope=scope,
                            constraint=after[(name, scope)].get("constraint"),
                        )
                    )
                for name, scope in sorted(set(before) - set(after)):
                    items.append(
                        self._item(
                            "dependency_removed",
                            f"{coord}:dependency_removed:{previous[0]}->"
                            f"{version}:{name}:{scope}",
                            coord,
                            source,
                            current,
                            previous[1],
                            from_version=previous[0],
                            version=version,
                            dependency=name,
                            scope=scope,
                        )
                    )
            previous = (version, current)
        return items

    def _repository_items(
        self, namespace: str, coord: str, source: str
    ) -> list[dict[str, Any]]:
        items = []
        for record in self.store.records(
            namespace,
            record_type="repository_link_assertion",
            source=source,
            coordinate=coord,
        ):
            chain = self._chain(record["record_id"])
            for earlier, later in zip(chain, chain[1:]):
                items.append(
                    self._item(
                        "repository_link_changed",
                        f"{coord}:repository_link_changed:{later['revision_id']}",
                        coord,
                        source,
                        later,
                        earlier,
                        links=[
                            x["repository_key"] for x in later["statement"]["links"]
                        ],
                    )
                )
        identity = OssIdentity(self.conn, initialize=False)
        for candidate in identity.candidates(
            namespace, scopes={"operator"}, kind="repository", key=package_key(coord)
        ):
            for step in candidate["history"]:
                if step["state"] in {"accepted", "reverted"} and step.get(
                    "decision_id"
                ):
                    items.append(
                        {
                            "id": f"{coord}:repository_link_changed:{step['decision_id']}",
                            "kind": "repository_link_changed",
                            "package": coord,
                            "source": "review",
                            "repository": candidate["right_key"].split(":", 1)[1],
                            "review_state": step["state"],
                            "decision_id": step["decision_id"],
                            "candidate_id": candidate["candidate_id"],
                        }
                    )
        return items

    def snapshot(self, subscription: dict[str, Any]) -> dict[str, Any]:
        namespace, query = subscription["namespace"], subscription["query"]
        if not self.store.ready():
            return {
                "items": [],
                "coverage": {"complete": False, "reason": "no OSS source has run"},
            }
        items = []
        for coord in self._packages(namespace, query):
            source = REGISTRY_SOURCES.get(coord.split(":")[1])
            if source is None:
                continue
            items += self._release_items(namespace, coord, source)
            items += self._licence_items(namespace, coord, source)
            items += self._dependency_items(namespace, coord, source)
            items += self._repository_items(namespace, coord, source)
        return {
            "items": sorted(items, key=lambda i: i["id"]),
            "coverage": {"complete": True},
        }

    # ------------------------------------------------------------ evaluation

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
        self.store.require_ready(namespace)
        if watermark is None:
            row = self.conn.execute(
                "SELECT max(watermark) FROM knowledge_subscription_watermarks WHERE namespace=?",
                [namespace],
            ).fetchone()
            if row is None or row[0] is None:
                raise OssStoreError(
                    "watermark_uncommitted",
                    "no committed watermark yet; source-pack runs and the "
                    "maintenance orchestrator commit them",
                )
            watermark = int(row[0])
        evaluated = self.subscriptions.evaluate(
            subscription_id,
            watermark,
            self.snapshot(subscription),
            principal_id=principal_id,
            scopes=scopes,
            observed_at_ms=self.now(),
        )
        notifications = []
        for event_id in evaluated.get("event_ids", []):
            event_type, key, after = self.conn.execute(
                "SELECT event_type, object_key, after_json FROM knowledge_subscription_events WHERE event_id=?",
                [event_id],
            ).fetchone()
            if event_type != "added" or not after:
                continue  # items are cumulative per stated change; only additions are news
            item = json.loads(after)
            notifications.append(
                {
                    "contract": CONTRACT,
                    "notification_id": f"{event_id}:{item['kind']}",
                    "event_id": event_id,
                    "kind": item["kind"],
                    "object": key,
                    "package": item["package"],
                    "message": self._message(item),
                    "cites": {
                        k: item[k]
                        for k in (
                            "source",
                            "old_revision_id",
                            "new_revision_id",
                            "decision_id",
                            "candidate_id",
                        )
                        if item.get(k)
                    },
                    "detail": {
                        k: v
                        for k, v in item.items()
                        if k not in {"id", "kind", "package"}
                    },
                }
            )
        return {
            "subscription_id": subscription_id,
            "status": evaluated["status"],
            "watermark": watermark,
            "notifications": notifications,
            "delivery": subscription["delivery"],
        }

    @staticmethod
    def _message(item: dict[str, Any]) -> str:
        where = (
            f"{item['package']}@{item.get('version')}"
            if item.get("version")
            else item["package"]
        )
        kind = item["kind"]
        if kind == "release_published":
            return f"{item['source']} published {where}."
        if kind in {"release_yanked", "release_deprecated", "release_unpublished"}:
            said = (
                f" Reason as published: {item['reason']}" if item.get("reason") else ""
            )
            return f"{item['source']} marked {where} {kind.split('_')[1]}.{said}"
        if kind == "licence_changed":
            return f"The declared licence of {where} differs from {item.get('from_version') or 'its earlier revision'}."
        if kind in {"dependency_added", "dependency_removed"}:
            verb = "adds" if kind == "dependency_added" else "removes"
            return f"{where} {verb} the {item['scope']} dependency {item['dependency']} (vs {item['from_version']})."
        return f"The repository link of {item['package']} changed ({item.get('review_state') or 'source assertion'})."

    def poll(
        self,
        subscription_id: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
        cursor: str = "",
    ) -> dict:
        scopes = set(scopes)
        self._subscription(subscription_id, principal_id, scopes)
        return self.subscriptions.poll(
            subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor
        )


__all__ = ["CONTRACT", "EVENT_KINDS", "WATCH_KINDS", "OssPackageMonitor"]
