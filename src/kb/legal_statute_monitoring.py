"""Watch federal statutes, provisions and amendment acts through ``platform.subscriptions`` (#2105, FL10).

A statute monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`) whose query names the watch
target - a statute, one provision of a statute, or an amendment act by its
BGBl citation. There is no monitor table and no scheduler: each evaluation
runs at a committed watermark (source-pack runs and the maintenance
orchestrator commit them), and the subscription store turns differences
between successive snapshots into events delivered through its poll/outbox
paths. Re-evaluating the same watermark, or a refresh that changed nothing,
produces no event.

Notifications describe source changes only:

* ``new_amendment_act`` - an acquired BGBl act with instructions touching the target;
* ``new_observed_version`` - an observed version whose (provision) text differs
  from the previous observed version;
* ``new_source_stated_version`` - a new or corrected source-stated version;
* ``new_citing_decision`` - a court decision explicitly citing the watched provision;
* ``dossier_linked`` - for a watched act, a Bundestag DIP dossier link.

Each cites the new and previous version or act with locators and, where both
versions exist, a provision-level diff. The first evaluation is a baseline:
its events are recorded but reported as ``in_view``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from src.kb.legal import READ_SCOPE, LegalError, LegalStore, _authorize
from src.kb.legal_citations import (
    bgbl_key,
    path_contains,
    statute_key,
    statute_registry,
)
from src.kb.legal_federal import OBSERVED, STATED, FederalStatutes

CONTRACT = "noesis-legal-statute-notification-v1"
WATCH_KINDS = ("statute", "provision", "amendment-act")


class StatuteMonitor:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = LegalStore(conn, initialize=initialize, now=now)
        self.federal = FederalStatutes(self.store)
        self.now = self.store.now
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    def create(
        self,
        namespace: str,
        request_key: str,
        *,
        watch: str,
        statute: str | None = None,
        provision: str | None = None,
        act: str | None = None,
        principal_id: str,
        scopes: Iterable[str],
        delivery: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        scopes = set(scopes)
        _authorize(namespace, scopes, READ_SCOPE, write=False)
        if watch not in WATCH_KINDS:
            raise LegalError("invalid_watch", f"watch one of {WATCH_KINDS}")
        query: dict[str, Any] = {
            "operation": "search",
            "kind": "statute-monitor",
            "watch": watch,
        }
        if watch in {"statute", "provision"}:
            if not str(statute or "").strip():
                raise LegalError(
                    "invalid_watch",
                    "a statute or provision watch names the statute (jurabk)",
                )
            query["statute"] = statute_key(statute)
            known = (
                {
                    statute_key(s["jurabk"])
                    for s in self.federal.registry_extra(namespace)
                }
                if self.federal.ready()
                else set()
            )
            if query["statute"] not in known | set(statute_registry()):
                raise LegalError(
                    "invalid_watch",
                    "the statute is neither in the bounded statute set nor acquired",
                )
            if watch == "provision":
                path, _ = self.federal._provision(
                    namespace, statute, str(provision or "")
                )
                query["provision"] = path
        else:
            key = _act_key(act)
            if key is None:
                raise LegalError(
                    "invalid_watch",
                    "an amendment-act watch names a BGBl citation "
                    "(e.g. 'BGBl. 2030 I Nr. 45' or 'bgbl-1/2030/nr-45')",
                )
            query["act"] = key
        created = self.subscriptions.create(
            {
                "namespace": namespace,
                "domain": "legal",
                "query": query,
                "filters": {"watch": watch},
                "cadence": {"trigger": "watermark"},
                "delivery": delivery or {"kind": "poll"},
            },
            "statute-monitor:" + request_key,
            principal_id=principal_id,
            scopes=scopes,
        )
        return {
            **created,
            "refresh": "source-pack runs and the maintenance orchestrator commit the watermarks this "
            "monitor evaluates; no separate scheduler",
        }

    def _subscription(
        self, subscription_id: str, principal_id: str, scopes: set[str]
    ) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(
            subscription_id, principal_id=principal_id, scopes=scopes
        )
        if subscription["query"].get("kind") != "statute-monitor":
            raise LegalError(
                "monitor_not_found", "subscription is not a statute monitor"
            )
        return subscription

    # ------------------------------------------------------------ snapshot

    def snapshot(self, subscription: dict[str, Any]) -> dict[str, Any]:
        namespace, query = subscription["namespace"], subscription["query"]
        if not self.federal.ready():
            return {
                "items": [],
                "coverage": {
                    "complete": False,
                    "reason": "no federal statute source acquired",
                },
            }
        items: list[dict[str, Any]] = []
        if query["watch"] == "amendment-act":
            act = next(
                (
                    a
                    for a in self.federal._acts(namespace).values()
                    if a["bgbl_key"] == query["act"]
                ),
                None,
            )
            if act:
                items.append(
                    {
                        "id": f"act:{act['work_id']}",
                        "kind": "amendment-act",
                        "work_id": act["work_id"],
                        "version_id": act["version_id"],
                        "bgbl_key": act["bgbl_key"],
                        "bgbl_citation": act["bgbl_citation"],
                        "promulgation_date": act["promulgation_date"],
                        "instructions": len(
                            self.federal._amendments(act["version_id"])
                        ),
                    }
                )
                for link in self.federal._dossier_links(namespace, act["work_id"]):
                    if link.get("dossier_id"):
                        items.append(
                            {
                                "id": f"dossier:{act['work_id']}:{link['dossier_id']}",
                                "kind": "dossier-link",
                                "bgbl_key": act["bgbl_key"],
                                "dossier_id": link["dossier_id"],
                                "evidence": link["evidence"],
                            }
                        )
            return {"items": items, "coverage": {"complete": True}}
        row = self.conn.execute(
            "SELECT work_id FROM legal_works WHERE namespace=? AND work_kind='statute' AND "
            "native_id=?",
            [namespace, f"statute:{query['statute']}"],
        ).fetchone()
        provision = query.get("provision")
        if row is not None:
            work_id = row[0]
            versions = [
                v for v in self.federal.versions(namespace, work_id) if v["current"]
            ]
            items += self._version_items(versions, provision)
            for act in self.federal.amendment_acts_for(namespace, work_id, provision):
                items.append(
                    {
                        "id": f"act:{act['work_id']}",
                        "kind": "amendment-act",
                        "work_id": act["work_id"],
                        "version_id": act["version_id"],
                        "bgbl_key": act["bgbl_key"],
                        "bgbl_citation": act["bgbl_citation"],
                        "promulgation_date": act["promulgation_date"],
                        "instructions": [
                            {
                                "provision": i["provision"],
                                "action": i["action"],
                                "locator": i["locator"],
                            }
                            for i in act["instructions"]
                        ],
                    }
                )
        for citation in self.conn.execute(
            "SELECT citation_id, decision_work_id, decision_date, provision, raw, locator_json FROM "
            "legal_provision_citations WHERE namespace=? AND statute_key=? ORDER BY citation_id",
            [namespace, query["statute"]],
        ).fetchall():
            if provision and not (
                path_contains(provision, citation[3])
                or path_contains(citation[3], provision)
            ):
                continue
            items.append(
                {
                    "id": f"cites:{citation[0]}",
                    "kind": "citing-decision",
                    "decision_work_id": citation[1],
                    "decision_date": str(citation[2]) if citation[2] else None,
                    "provision": citation[3],
                    "raw": citation[4],
                    "locator": json.loads(citation[5]),
                }
            )
        return {"items": items, "coverage": {"complete": True}}

    def _version_items(
        self, versions: list[dict[str, Any]], provision: str | None
    ) -> list[dict[str, Any]]:
        items = []
        observed = sorted(
            (v for v in versions if v["validity_basis"] == OBSERVED),
            key=lambda v: (
                min(v["sightings_ms"] or [v["first_observed_at_ms"]]),
                v["version_id"],
            ),
        )
        previous = None
        for version in observed:
            fingerprint = self._fingerprint(version, provision)
            # Only an observed version whose (provision) text changed is an event; unchanged re-observations are not.
            if previous is None or fingerprint != previous[1]:
                items.append(
                    {
                        "id": f"version:{version['version_id']}",
                        "kind": "observed-version",
                        "version_id": version["version_id"],
                        "fingerprint": fingerprint,
                        "observed_on": version["observed_on"][0]
                        if version["observed_on"]
                        else None,
                        "previous_version_id": previous[0] if previous else None,
                    }
                )
                previous = (version["version_id"], fingerprint)
        stated = sorted(
            (v for v in versions if v["validity_basis"] == STATED),
            key=lambda v: (v["validity_from"] or "", v["version_id"]),
        )
        prior = None
        for version in stated:
            items.append(
                {
                    "id": f"version:{version['native_id']}",
                    "kind": "source-stated-version",
                    "version_id": version["version_id"],
                    "eli": version["native_id"],
                    "validity_from": version["validity_from"],
                    "validity_to": version["validity_to"],
                    "fingerprint": self._fingerprint(version, provision),
                    "corrects_version_id": version["corrects_version_id"],
                    "previous_version_id": prior,
                }
            )
            prior = version["version_id"]
        return items

    def _fingerprint(
        self, version: dict[str, Any], provision: str | None
    ) -> str | None:
        if provision is None:
            return version["text_sha256"]
        passages, _ = self.federal.provision_passages(version["version_id"], provision)
        return self.federal._provision_sha(passages)

    # ----------------------------------------------------------------- run

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
        _authorize(namespace, scopes, READ_SCOPE, write=False)
        if watermark is None:
            row = self.conn.execute(
                "SELECT max(watermark) FROM knowledge_subscription_watermarks WHERE namespace=?",
                [namespace],
            ).fetchone()
            if row is None or row[0] is None:
                raise LegalError(
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
        notifications = []
        for event_id in evaluated.get("event_ids", []):
            row = self.conn.execute(
                "SELECT event_type, object_key, before_json, after_json FROM "
                "knowledge_subscription_events WHERE event_id=?",
                [event_id],
            ).fetchone()
            notifications.extend(
                self._classify(
                    namespace, subscription["query"], event_id, *row, baseline=baseline
                )
            )
        return {
            "subscription_id": subscription_id,
            "status": evaluated["status"],
            "watermark": watermark,
            "baseline": baseline,
            "notifications": notifications,
            "delivery": subscription["delivery"],
        }

    def _classify(
        self,
        namespace: str,
        query: dict[str, Any],
        event_id: str,
        event_type: str,
        key: str,
        before: str | None,
        after: str | None,
        *,
        baseline: bool,
    ) -> list[dict[str, Any]]:
        before_item = json.loads(before) if before else None
        after_item = json.loads(after) if after else None
        if event_type == "removed" or after_item is None:
            return []
        item = after_item

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

        if (
            event_type == "changed"
            and before_item
            and before_item.get("version_id") == item.get("version_id")
            and item["kind"] != "source-stated-version"
        ):
            return []
        if item["kind"] == "amendment-act":
            return [
                note(
                    "new_amendment_act",
                    f"{item['bgbl_citation']} (promulgated {item['promulgation_date']}) "
                    "states amending instructions for the watched target.",
                    {
                        "act_work_id": item["work_id"],
                        "act_version_id": item["version_id"],
                        "bgbl_key": item["bgbl_key"],
                        "instructions": item["instructions"],
                    },
                )
            ]
        if item["kind"] == "dossier-link":
            return [
                note(
                    "dossier_linked",
                    f"{item['bgbl_key']} is linked to dossier {item['dossier_id']}.",
                    {"dossier_id": item["dossier_id"], "evidence": item["evidence"]},
                )
            ]
        if item["kind"] == "citing-decision":
            return [
                note(
                    "new_citing_decision",
                    f"A decision of {item['decision_date']} cites {item['raw']}.",
                    {
                        "decision_work_id": item["decision_work_id"],
                        "provision": item["provision"],
                        "locator": item["locator"],
                    },
                )
            ]
        previous = item.get("previous_version_id")
        if item["kind"] == "source-stated-version" and before_item:
            previous = before_item["version_id"]
            if previous == item["version_id"]:
                return []
        cites: dict[str, Any] = {
            "version_id": item["version_id"],
            "previous_version_id": previous,
        }
        if previous and query.get("provision"):
            try:
                cites["diff"] = self.federal.compare_provision(
                    namespace,
                    query["statute"],
                    query["provision"],
                    previous,
                    item["version_id"],
                    scopes={READ_SCOPE, f"namespace:{namespace}:read"},
                )["changes"]
            except LegalError as exc:  # a version without the provision, etc.
                cites["diff_unavailable"] = exc.code
        elif previous:
            cites["diff"] = self.store.compare_versions(
                namespace,
                previous,
                item["version_id"],
                scopes={READ_SCOPE, f"namespace:{namespace}:read"},
            )["changes"]
        if item["kind"] == "observed-version":
            return [
                note(
                    "new_observed_version",
                    f"Changed text observed on {item['observed_on']} "
                    "(validity not stated).",
                    cites,
                )
            ]
        label = (
            "corrected"
            if item.get("corrects_version_id") or event_type == "changed"
            else "new"
        )
        return [
            note(
                "new_source_stated_version",
                f"The source states a {label} version valid from "
                f"{item['validity_from']}.",
                {**cites, "eli": item["eli"]},
            )
        ]

    def poll(
        self,
        subscription_id: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
        cursor: str = "",
    ) -> dict[str, Any]:
        scopes = set(scopes)
        present = self.conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='knowledge_subscriptions'"
        ).fetchone()
        if not present:
            raise LegalError("not_ready", "no statute monitor has been created yet")
        self._subscription(subscription_id, principal_id, scopes)
        return self.subscriptions.poll(
            subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor
        )


def _act_key(value: Any) -> str | None:
    from src.kb.legal_citations import parse_bgbl_references

    text = str(value or "").strip()
    if text.startswith("bgbl-"):
        parts = text.split("/")
        if len(parts) == 3 and parts[2].startswith(("nr-", "s-")):
            return text
    refs = [r for r in parse_bgbl_references(text) if r["key"]]
    if len(refs) == 1:
        return refs[0]["key"]
    from src.ingestion.federal_law_formats import bgbl_key_from_eli

    return bgbl_key_from_eli(text) or (
        bgbl_key(*text.split(":")) if text.count(":") == 2 else None
    )
