"""Monitor new notices and notice updates through ``platform.subscriptions`` (#1916, R09 #2020).

A product-notice monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`) whose query names what is
watched: Products model or variant ids, brand or GTIN strings, or issuing
authorities. There is no monitor table, queue or scheduler of its own; the
``products-displays`` source pack's schedule refreshes the notice sources.

Each evaluation lists cumulative items, one per event, each citing the notice
revision and (for products) the match decision:

* ``notice_attached`` / ``notice_detached`` - a match review that attaches a
  notice to a watched product, or reverses that (or a later revision that no
  longer names the product);
* ``new_notice`` - the first revision of a notice naming a watched brand or
  GTIN string, or issued by a watched authority; a brand-level item says
  whether the identification is matched to a product or is an ``unmatched
  identification``;
* ``notice_revised`` - a later revision that became current by the source's
  own date, flagging a changed corrective action or hazard text. A late,
  older payload kept as history is never a revision event.

Watermarks: a monitor evaluates at the latest committed ``products-displays``
source-pack watermark whose run completed *every* notice source it ran, with
only the revisions projected up to that run. A partial or failed run is never
evaluated, so its revisions surface with the next complete run instead of
leaving a silent gap. The subscription watermark combines that source
watermark with the review generation, so a review between runs is evaluated
too and replaying either adds nothing. A sibling, size or regional variant of
a watched model is never attached, so it produces no event for that model.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.product_safety import (
    READ_SCOPE,
    SOURCE_PACK,
    ProductSafetyError,
    ProductSafetyStore,
    _brand_key,
    _date_rank,
    _table,
    authorize,
    gtin_key,
)

CONTRACT = "noesis-product-notice-notification-v1"
EVENT_KINDS = ("notice_attached", "notice_detached", "new_notice", "notice_revised")
AUTHORITIES = ("eu-safety-gate", "us-cpsc", "us-nhtsa", "eu-rasff")
# Subscription watermark = source-pack watermark * GENERATION_SPAN + review generation (10**9): the
# source watermark of the last complete notice run, plus the namespace's append-only count of match,
# review and party-link changes, so a review between runs is a new, monotone watermark and replaying
# either part adds nothing. The committed detail records both parts and the notice cutoff sequence.
GENERATION_SPAN = 1_000_000_000


class ProductNoticeMonitor:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = ProductSafetyStore(conn, initialize=initialize, now=self.now)
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    # ---------------------------------------------------------------- create

    def _watch(self, namespace: str, watch: Mapping[str, Any]) -> dict[str, list[str]]:
        watch = dict(watch or {})
        allowed = {"models", "variants", "brands", "gtins", "authorities"}
        if set(watch) - allowed or not any(watch.get(k) for k in allowed):
            raise ProductSafetyError(
                "invalid_watch",
                "watch models, variants, brands, gtins or authorities (at least one)",
            )
        result = {
            k: sorted({str(v).strip() for v in watch.get(k) or [] if str(v).strip()})
            for k in sorted(allowed)
        }
        if set(result["authorities"]) - set(AUTHORITIES):
            raise ProductSafetyError(
                "invalid_watch", f"authorities are one of {AUTHORITIES}"
            )
        for identity in result["models"] + result["variants"]:
            row = (
                self.conn.execute(
                    "SELECT level FROM product_identities WHERE namespace=? AND identity_id=?",
                    [namespace, identity],
                ).fetchone()
                if _table(self.conn, "product_identities")
                else None
            )
            expected = "model" if identity in result["models"] else "variant"
            if row is None or row[0] != expected:
                raise ProductSafetyError(
                    "not_found", f"{identity} is not a visible Products {expected}"
                )
        return result

    def create(
        self,
        namespace: str,
        request_key: str,
        *,
        watch: Mapping[str, Any],
        principal_id: str,
        scopes: Iterable[str],
        delivery: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        normalized = self._watch(namespace, watch)
        created = self.subscriptions.create(
            {
                "namespace": namespace,
                "domain": "products",
                "query": {
                    "operation": "search",
                    "kind": "product-notice-monitor",
                    "watch": normalized,
                },
                "filters": {"watch": sorted(k for k, v in normalized.items() if v)},
                "cadence": {"trigger": "watermark", "source_pack": SOURCE_PACK},
                "delivery": delivery or {"kind": "poll"},
            },
            "product-notice-monitor:" + request_key,
            principal_id=principal_id,
            scopes=scopes,
        )
        return {
            **created,
            "refresh": f"evaluated at complete {SOURCE_PACK} source-pack runs of the notice sources; "
            "no separate scheduler",
        }

    def _subscription(
        self, subscription_id: str, principal_id: str, scopes: set[str]
    ) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(
            subscription_id, principal_id=principal_id, scopes=scopes
        )
        if subscription["query"].get("kind") != "product-notice-monitor":
            raise ProductSafetyError(
                "monitor_not_found", "subscription is not a product-notice monitor"
            )
        return subscription

    # ---------------------------------------------------------------- watermark

    def complete_watermark(
        self, namespace: str, watermark: int | None = None
    ) -> dict[str, Any]:
        """The latest (or the named) source-pack watermark whose run completed every notice source it ran."""
        if not _table(self.conn, "source_pack_watermarks") or not _table(
            self.conn, "product_safety_source_runs"
        ):
            raise ProductSafetyError(
                "watermark_uncommitted", "no committed notice run yet"
            )
        rows = self.conn.execute(
            "SELECT watermark, committed_at_ms, run_id FROM source_pack_watermarks WHERE pack_id=? "
            "AND (? IS NULL OR watermark=?) ORDER BY watermark DESC",
            [SOURCE_PACK, watermark, watermark],
        ).fetchall()
        for mark, committed_at, run_id in rows:
            runs = self.conn.execute(
                "SELECT source_id, status, cutoff_seq FROM product_safety_source_runs WHERE namespace=? AND run_id=?",
                [namespace, run_id],
            ).fetchall()
            if not runs:
                continue  # a display-only run: nothing about notices to evaluate
            if any(status != "complete" for _, status, _ in runs):
                if watermark is not None:
                    raise ProductSafetyError(
                        "incomplete_run",
                        "that run did not complete every notice source; it is never evaluated",
                        failed=sorted(
                            s for s, status, _ in runs if status != "complete"
                        ),
                    )
                continue
            return {
                "source_watermark": int(mark),
                "committed_at_ms": int(committed_at),
                "run_id": run_id,
                "cutoff_seq": max(int(c) for _, _, c in runs),
            }
        raise ProductSafetyError(
            "watermark_uncommitted",
            "no complete notice-source run is committed yet; partial runs are never evaluated",
        )

    # ---------------------------------------------------------------- snapshot

    def _chain(
        self, namespace: str, notice_id: str, cutoff: int
    ) -> list[dict[str, Any]]:
        """Revisions that became current when they arrived (by the source's own date), up to the cutoff."""
        chain, best = [], None
        for revision in self.store.revisions(namespace, notice_id):
            if revision["seq"] > cutoff:
                continue
            rank = _date_rank(revision["revision_date"])
            if best is None or rank >= best:
                chain.append(revision)
                best = rank
        return chain

    def _texts(self, namespace: str, revision_id: str) -> tuple[list, list]:
        parts = self.store.parts(namespace, revision_id)
        return (
            [a["text"] for a in parts["corrective_actions"]],
            [(h["hazard_type"], h["description"]) for h in parts["hazards"]],
        )

    def _cite(
        self, namespace: str, notice_id: str, revision: Mapping[str, Any]
    ) -> dict[str, Any]:
        head = self.store._notice_row(namespace, notice_id)
        return {
            "notice_id": notice_id,
            "provider": head["provider"],
            "notice_number": head["notice_number"],
            "revision_id": revision["revision_id"],
            "revision_no": revision["revision_no"],
            "revision_date": revision["revision_date"],
            "authority": revision["authority"],
        }

    def _revision_items(
        self,
        namespace: str,
        notice_id: str,
        cutoff: int,
        subject: str,
        extra: Mapping[str, Any],
        *,
        first_kind: str | None,
        after_seq: int = 0,
    ) -> list[dict[str, Any]]:
        items = []
        chain = self._chain(namespace, notice_id, cutoff)
        previous = None
        for index, revision in enumerate(chain):
            texts = self._texts(namespace, revision["revision_id"])
            if index == 0 and first_kind:
                items.append(
                    {
                        "id": f"{subject}:{first_kind}:{revision['revision_id']}",
                        "kind": first_kind,
                        **self._cite(namespace, notice_id, revision),
                        **extra,
                    }
                )
            elif index > 0 and revision["seq"] > after_seq:
                items.append(
                    {
                        "id": f"{subject}:notice_revised:{revision['revision_id']}",
                        "kind": "notice_revised",
                        **self._cite(namespace, notice_id, revision),
                        "previous_revision_id": chain[index - 1]["revision_id"],
                        "corrective_action_changed": texts[0] != previous[0],
                        "hazard_changed": texts[1] != previous[1],
                        **extra,
                    }
                )
            previous = texts
        return items

    def snapshot(self, subscription: Mapping[str, Any], cutoff: int) -> dict[str, Any]:
        namespace, watch = subscription["namespace"], subscription["query"]["watch"]
        items: list[dict[str, Any]] = []
        models: set[str] = set()
        for identity in watch["models"] + watch["variants"]:
            # The same "models equivalent to X" as lookup_product_notices: accepted cross-provider equivalents.
            try:
                models.update(self.store.equivalent_models(namespace, identity))
            except ProductSafetyError:
                continue  # a watched identity no longer visible watches nothing
        for match in self.store.matches_for_models(namespace, models):
            events, attached, since = self._match_state(namespace, match, cutoff)
            items += events
            if attached:
                items += self._revision_items(
                    namespace,
                    match["notice_id"],
                    cutoff,
                    f"match:{match['match_id']}",
                    {
                        "model_id": match["model_id"],
                        "match": {"match_id": match["match_id"]},
                    },
                    first_kind=None,
                    after_seq=since,
                )
        brands = {_brand_key(b) for b in watch["brands"]}
        gtins = {gtin_key(g) or f"raw:{g}" for g in watch["gtins"]}
        authorities = set(watch["authorities"])
        if brands or gtins or authorities:
            for (notice_id,) in self.conn.execute(
                "SELECT notice_id FROM product_safety_notices WHERE namespace=? ORDER BY notice_id",
                [namespace],
            ).fetchall():
                chain = self._chain(namespace, notice_id, cutoff)
                if not chain:
                    continue
                for label, hit in self._string_hits(
                    namespace, chain, brands, gtins, authorities
                ):
                    attached = [
                        m
                        for m in self.store.matches_for_notice(namespace, notice_id)
                        if self._match_state(namespace, m, cutoff)[1]
                    ]
                    extra = {
                        "watched": label,
                        "identification": "matched to a product"
                        if attached
                        else "unmatched identification",
                        "matched": hit,
                    }
                    items += self._revision_items(
                        namespace,
                        notice_id,
                        cutoff,
                        f"{label}:{notice_id}",
                        extra,
                        first_kind="new_notice",
                    )
        return {"items": items, "coverage": {"complete": True}}

    def _match_state(
        self, namespace: str, match: Mapping[str, Any], cutoff: int
    ) -> tuple[list[dict[str, Any]], bool, int]:
        """Attach/detach events and the attachment of one match, seen only through revisions up to the cutoff.

        A review cites the revision current when it was made; a review of a revision projected after the last
        complete run (or a candidate state drawn from such a revision) waits for the next complete run.
        """
        revisions = {
            r["revision_id"]: r
            for r in self.store.revisions(namespace, match["notice_id"])
        }

        def visible(revision_id: str) -> bool:
            return revision_id in revisions and revisions[revision_id]["seq"] <= cutoff

        events: list[dict[str, Any]] = []
        attached, since = False, 0
        for review in match["review_history"]:
            if not visible(review["revision_id"]):
                continue
            now_attached = (
                review["decision"] == "accepted"
                and review["candidate_state"] == "proposed"
            )
            if now_attached != attached:
                kind = "notice_attached" if now_attached else "notice_detached"
                events.append(
                    {
                        "id": f"match:{match['match_id']}:{kind}:{review['sequence']}",
                        "kind": kind,
                        **self._cite(
                            namespace,
                            match["notice_id"],
                            revisions[review["revision_id"]],
                        ),
                        "model_id": match["model_id"],
                        "match": {
                            "match_id": match["match_id"],
                            "review_sequence": review["sequence"],
                            "decision": review["decision"],
                            "reason": review["reason"],
                        },
                    }
                )
                attached = now_attached
                since = revisions[review["revision_id"]]["seq"]
        if (
            attached
            and visible(match["revision_id"])
            and match["candidate_state"] != "proposed"
        ):
            # Accepted, but a revision within the cutoff no longer names the product.
            events.append(
                {
                    "id": f"match:{match['match_id']}:notice_detached:{match['candidate_state']}:"
                    f"{match['revision_id']}",
                    "kind": "notice_detached",
                    **self._cite(
                        namespace, match["notice_id"], revisions[match["revision_id"]]
                    ),
                    "model_id": match["model_id"],
                    "match": {
                        "match_id": match["match_id"],
                        "candidate_state": match["candidate_state"],
                    },
                }
            )
            attached = False
        return events, attached, since

    def _string_hits(self, namespace, chain, brands, gtins, authorities):
        """Watched strings a notice names in any current-chain revision (first naming wins the label)."""
        hits = []
        for revision in chain:
            if revision["authority"] in authorities:
                hits.append(
                    (
                        f"authority:{revision['authority']}",
                        {"authority": revision["authority"]},
                    )
                )
            for group in self.store._groups(
                namespace, revision["revision_id"]
            ).values():
                for key in brands & set(group["brand"]):
                    hits.append((f"brand:{key}", {"brand": group["brand"][key]}))
                for key in gtins & set(group["gtin"]):
                    hits.append((f"gtin:{key}", {"gtin": group["gtin"][key]}))
        seen, result = set(), []
        for label, hit in hits:
            if label not in seen:
                seen.add(label)
                result.append((label, hit))
        return result

    # ---------------------------------------------------------------- run

    def run(
        self,
        subscription_id: str,
        watermark: int | None = None,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Evaluate at a complete notice run (``watermark`` is a source-pack watermark) and the review generation."""
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        namespace = subscription["namespace"]
        authorize(namespace, scopes, READ_SCOPE)
        mark = self.complete_watermark(namespace, watermark)
        generation = self.store.generation(namespace)
        if generation >= GENERATION_SPAN:
            raise ProductSafetyError(
                "generation_overflow", "review generation exceeds the watermark span"
            )
        combined = mark["source_watermark"] * GENERATION_SPAN + generation
        self.subscriptions.commit_watermark(
            namespace,
            combined,
            kind="ingestion",
            detail={
                "source_pack": SOURCE_PACK,
                "source_watermark": mark["source_watermark"],
                "review_generation": generation,
                "notice_cutoff_seq": mark["cutoff_seq"],
            },
            committed_at_ms=mark["committed_at_ms"],
        )
        evaluated = self.subscriptions.evaluate(
            subscription_id,
            combined,
            self.snapshot(subscription, mark["cutoff_seq"]),
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
                continue  # items are cumulative per revision and review; only additions are news
            item = json.loads(after)
            notifications.append(
                {
                    "contract": CONTRACT,
                    "notification_id": f"{event_id}:{item['kind']}",
                    "event_id": event_id,
                    "kind": item["kind"],
                    "object": key,
                    "message": _message(item),
                    "cites": {
                        k: item.get(k)
                        for k in (
                            "notice_id",
                            "provider",
                            "notice_number",
                            "revision_id",
                            "revision_no",
                            "revision_date",
                            "authority",
                            "previous_revision_id",
                        )
                        if item.get(k) is not None
                    },
                    **{
                        k: item[k]
                        for k in (
                            "model_id",
                            "match",
                            "watched",
                            "identification",
                            "matched",
                            "corrective_action_changed",
                            "hazard_changed",
                        )
                        if k in item
                    },
                }
            )
        return {
            "subscription_id": subscription_id,
            "status": evaluated["status"],
            "watermark": combined,
            "source_watermark": mark["source_watermark"],
            "review_generation": generation,
            "run_id": mark["run_id"],
            "notifications": notifications,
            "delivery": subscription["delivery"],
        }

    def poll(
        self,
        subscription_id: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
        cursor: str = "",
    ) -> dict[str, Any]:
        scopes = set(scopes)
        if not self.store.ready() or not _table(self.conn, "knowledge_subscriptions"):
            raise ProductSafetyError(
                "not_ready", "no product-notice monitor or notice run exists yet"
            )
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(
            subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor
        )


def _message(item: Mapping[str, Any]) -> str:
    notice = f"{item['provider']} {item['notice_number']}"
    if item["kind"] == "notice_attached":
        return f"A reviewer attached {notice} to the watched product (revision {item['revision_no']})."
    if item["kind"] == "notice_detached":
        return f"{notice} is no longer attached to the watched product; its history is kept."
    if item["kind"] == "new_notice":
        return f"{notice} names a watched {item['watched'].split(':', 1)[0]} ({item['identification']})."
    changed = [
        name
        for flag, name in (
            ("corrective_action_changed", "corrective action"),
            ("hazard_changed", "hazard text"),
        )
        if item.get(flag)
    ]
    return f"{notice} has a new revision ({item['revision_date']})" + (
        f"; changed: {', '.join(changed)}." if changed else "."
    )


__all__ = ["CONTRACT", "EVENT_KINDS", "ProductNoticeMonitor"]
