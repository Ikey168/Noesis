"""Monitor transaction releases, index vintages and parcel revisions through subscriptions (#2228, RE10 #2506).

A real-estate monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`), exactly like the housing
monitors (:mod:`src.kb.housing_monitoring`): no monitor table, queue or
scheduler of its own. It follows **places** (a Geospatial place id or published
place codes) and **parcels** (a national cadastral reference). The
``geospatial-real-estate`` source pack's schedule re-reads the bounded
selections within the RE01 budgets through the existing runtime scheduler; a
monitor evaluates only at a committed source-pack watermark whose run completed
every real-estate source it ran.

Event types, each carrying record and revision ids and the source release:

* ``transaction_new`` - the first revision of a transaction touching the place
  or parcel;
* ``transaction_revised`` - a PPD change row or a changed DVF mutation;
* ``transaction_withdrawn`` - a PPD deletion row or a DVF mutation absent from a
  later release (history kept);
* ``index_vintage_new`` - a new release of an index covering the place's
  geography code;
* ``parcel_revised`` - a changed parcel geometry or reference.

Each item is cumulative per revision or vintage, so an unchanged release emits
nothing and a replay at the same watermark delivers nothing. Notification text
says what was published and where; it never characterises a change as a price
trend, a movement or a recommendation.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.real_estate import READ_SCOPE, RealEstateError, RealEstateStore, authorize, table_exists
from src.kb.real_estate_identity import PLACE_SCHEMES, RealEstateIdentity, places

KIND = "real-estate-monitor"
SOURCE_PACK = "geospatial-real-estate"
NOTIFICATION_CONTRACT = "noesis-real-estate-notification-v1"
EVENT_KINDS = ("transaction_new", "transaction_revised", "transaction_withdrawn", "index_vintage_new",
               "parcel_revised")
GENERATION_SPAN = 1_000_000_000
WORDING = "states what was published and where; not a price trend, valuation or recommendation"


class RealEstateMonitor:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = RealEstateStore(conn, initialize=initialize, now=self.now)
        self.identity = RealEstateIdentity(conn, initialize=initialize, now=self.now)
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    def create(self, namespace: str, request_key: str, *, principal_id: str, scopes: Iterable[str],
               places: list[str] | None = None, codes: list[Mapping[str, str]] | None = None,
               parcels: list[str] | None = None, delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if not (places or codes or parcels):
            raise RealEstateError("invalid_watch", "watch at least one place, place code or parcel")
        known = {p["place_id"] for p in _places(self.conn, namespace)}
        for place_id in places or []:
            if place_id not in known:
                raise RealEstateError("not_found", f"place {place_id!r} is not visible")
        for code in codes or []:
            if code.get("scheme") not in PLACE_SCHEMES or not code.get("code"):
                raise RealEstateError("invalid_watch", f"place code schemes are {PLACE_SCHEMES}")
        watch = {"places": sorted(set(places or [])),
                 "codes": sorted({f"{c['scheme']}:{str(c['code']).upper()}" for c in codes or []}),
                 "parcels": sorted(set(parcels or []))}
        created = self.subscriptions.create(
            {"namespace": namespace, "domain": "geospatial",
             "query": {"operation": "search", "kind": KIND, "watch": watch},
             "filters": {"watch": ["places", "codes", "parcels"]},
             "cadence": {"trigger": "watermark", "source_pack": SOURCE_PACK},
             "delivery": delivery or {"kind": "poll"}},
            "real-estate-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        return {**created, "refresh": f"evaluated at complete {SOURCE_PACK} runs within the RE01 budgets through the "
                                      "existing source-pack scheduler; no separate scheduler"}

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != KIND:
            raise RealEstateError("monitor_not_found", "subscription is not a real-estate monitor")
        return subscription

    def complete_watermark(self, namespace: str, watermark: int | None = None) -> dict[str, Any]:
        if not table_exists(self.conn, "source_pack_watermarks") or not table_exists(self.conn,
                                                                                      "real_estate_source_runs"):
            raise RealEstateError("watermark_uncommitted", "no committed real-estate run yet")
        rows = self.conn.execute(
            "SELECT watermark, committed_at_ms, run_id FROM source_pack_watermarks WHERE pack_id=? AND "
            "(? IS NULL OR watermark=?) ORDER BY watermark DESC", [SOURCE_PACK, watermark, watermark]).fetchall()
        for mark, committed_at, run_id in rows:
            runs = self.store.runs(namespace, run_id)
            if not runs:
                continue
            if any(r["status"] != "complete" for r in runs):
                if watermark is not None:
                    raise RealEstateError("incomplete_run", "that run did not complete every source it ran")
                continue
            return {"source_watermark": int(mark), "committed_at_ms": int(committed_at), "run_id": run_id,
                    "cutoff_seq": max(r["cutoff_seq"] for r in runs)}
        raise RealEstateError("watermark_uncommitted", "no complete real-estate run is committed yet")

    # ------------------------------------------------------------------ snapshot

    def _watched_codes(self, namespace: str, watch: Mapping[str, Any]) -> dict[str, set[tuple[str, str]]]:
        known = {p["place_id"]: p for p in _places(self.conn, namespace)}
        out = {f"place:{pid}": {(s, c.upper()) for s, c in known[pid]["source_ids"].items() if s in PLACE_SCHEMES}
               for pid in watch.get("places") or [] if pid in known}
        for code in watch.get("codes") or []:
            scheme, value = code.split(":", 1)
            out[f"code:{code}"] = {(scheme, value)}
        return out

    @staticmethod
    def _cite(revision: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if revision is None:
            return None
        source = revision["statement"]["source"]
        return {"revision_id": revision["revision_id"], "record_id": revision["record_id"],
                "event": revision["event"], "release": revision["release"], "published_on": revision["published_on"],
                "url": source.get("url"), "publisher": source.get("publisher")}

    def snapshot(self, subscription: Mapping[str, Any], cutoff: int) -> dict[str, Any]:
        namespace = subscription["namespace"]
        watch = subscription["query"]["watch"]
        watched = self._watched_codes(namespace, watch)
        parcel_ids = {}
        for record in self.store.records(namespace, record_type="parcel"):
            revisions = self.store.revisions(namespace, record["record_id"], cutoff_seq=cutoff)
            if revisions and revisions[-1]["statement"]["as_published"]["national_cadastral_reference"] in set(
                    watch.get("parcels") or []):
                parcel_ids[record["record_id"]] = revisions[-1]["statement"]["as_published"][
                    "national_cadastral_reference"]
        items = []

        def add(target, kind, key, prior, new, **extra):
            items.append({"id": f"{target}:{kind}:{key}", "kind": kind, "watched": target, "prior": prior,
                          "new": new, "wording": WORDING, **extra})

        for record in self.store.records(namespace):
            revisions = self.store.revisions(namespace, record["record_id"], cutoff_seq=cutoff)
            if not revisions:
                continue
            if record["record_type"] == "transaction":
                for index, revision in enumerate(revisions):
                    statement = revision["statement"]
                    refs = {(r["scheme"], str(r["code"]).upper()) for r in statement["place_refs"]}
                    targets = [t for t, codes in watched.items() if refs & codes]
                    parcel_refs = {r["code"] for r in statement["parcel_refs"]}
                    targets += [f"parcel:{ref}" for ref in parcel_refs & set(watch.get("parcels") or [])]
                    matched = {m["target_id"] for m in self.identity.usable(namespace, record["record_id"], "parcel")}
                    targets += [f"parcel:{parcel_ids[p]}" for p in matched & set(parcel_ids)
                                if parcel_ids[p] not in parcel_refs]
                    kind = ("transaction_withdrawn" if revision["event"] in {"withdrawn", "removed"} else
                            "transaction_new" if index == 0 else "transaction_revised")
                    for target in sorted(set(targets)):
                        add(target, kind, revision["revision_id"],
                            self._cite(revisions[index - 1]) if index else None, self._cite(revision),
                            transaction=statement["record_key"], provider=statement["provider"])
            elif record["record_type"] == "parcel" and record["record_id"] in parcel_ids:
                for index, revision in enumerate(revisions[1:], start=1):
                    add(f"parcel:{parcel_ids[record['record_id']]}", "parcel_revised", revision["revision_id"],
                        self._cite(revisions[index - 1]), self._cite(revision))
        for vintage in self.store.vintages(namespace, cutoff_seq=cutoff):
            if vintage["provider"] not in {"hmlr-ukhpi", "eurostat-hpi"}:
                continue
            covered = self._vintage_codes(namespace, vintage)
            for target, codes in watched.items():
                if covered & codes:
                    add(target, "index_vintage_new", vintage["vintage_id"], None,
                        {"vintage_id": vintage["vintage_id"], "source_id": vintage["source_id"],
                         "release": vintage["release"], "published_on": vintage["published_on"],
                         "records": vintage["record_count"], "changed": vintage["changed"]},
                        provider=vintage["provider"])
        return {"items": sorted(items, key=lambda i: i["id"]), "coverage": {"complete": True}}

    def _vintage_codes(self, namespace: str, vintage: Mapping[str, Any]) -> set[tuple[str, str]]:
        rows = self.conn.execute(
            "SELECT DISTINCT statement_json FROM real_estate_revisions WHERE namespace=? AND source_id=? AND "
            "run_id=?", [namespace, vintage["source_id"], vintage["run_id"]]).fetchall()
        codes = set()
        for (text,) in rows:
            geography = json.loads(text)["as_published"].get("geography") or {}
            codes.add((geography.get("scheme"), str(geography.get("code")).upper()))
        if not codes:  # a vintage that changed nothing still covers the geographies its source declares
            for record in self.store.records(namespace, record_type="price_index_observation",
                                             provider=vintage["provider"]):
                geography = self.store.current(namespace, record["record_id"])["statement"]["as_published"][
                    "geography"]
                codes.add((geography["scheme"], str(geography["code"]).upper()))
        return codes

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
                continue
            item = json.loads(after)
            notifications.append({"contract": NOTIFICATION_CONTRACT, "notification_id": f"{event_id}:{item['kind']}",
                                  "event_id": event_id, "kind": item["kind"], "object": key,
                                  "watched": item["watched"], "prior": item["prior"], "new": item["new"],
                                  "provider": item.get("provider"), "transaction": item.get("transaction"),
                                  "wording": WORDING, "baseline": baseline})
        return {"subscription_id": subscription_id, "status": evaluated["status"], "watermark": combined,
                "source_watermark": mark["source_watermark"], "run_id": mark["run_id"], "baseline": baseline,
                "notifications": notifications, "delivery": subscription["delivery"]}

    def poll(self, subscription_id: str, *, principal_id: str, scopes: Iterable[str], cursor: str = "") -> dict:
        scopes = set(scopes)
        if not table_exists(self.conn, "knowledge_subscriptions"):
            raise RealEstateError("not_ready", "no real-estate monitor exists yet")
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)


def _places(conn: Any, namespace: str) -> list[dict[str, Any]]:
    return places(conn, namespace)


__all__ = ["EVENT_KINDS", "KIND", "NOTIFICATION_CONTRACT", "RealEstateMonitor", "SOURCE_PACK"]
