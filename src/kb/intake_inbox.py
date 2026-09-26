"""Owner-scoped intake subscriptions, source snapshots, and durable triage state."""

from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import quote, urlsplit

from src.ingestion.provider_execution import ProviderError, _safe_url
from src.ingestion.source_packs import SourcePackError, _validate_endpoint
from src.kb.intake_modes import (
    DECISIONS,
    IntakeError,
    IntakeStore,
    _bounded,
    _hash,
    _json,
    _text,
)

_DDL = """
CREATE TABLE IF NOT EXISTS intake_inbox_subscriptions(
 subscription_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL,
 source_kind TEXT NOT NULL, url TEXT NOT NULL, name TEXT NOT NULL,
 enabled BOOLEAN NOT NULL, created_at_ms BIGINT NOT NULL, updated_at_ms BIGINT NOT NULL,
 UNIQUE(namespace,owner,url)
);
CREATE TABLE IF NOT EXISTS intake_inbox_items(
 namespace TEXT NOT NULL, owner TEXT NOT NULL, item_id TEXT NOT NULL,
 source_ids_json TEXT NOT NULL, original_url TEXT NOT NULL, title TEXT NOT NULL,
 content TEXT NOT NULL, published_at_ms BIGINT, first_seen_ms BIGINT NOT NULL,
 last_seen_ms BIGINT NOT NULL, source_version BIGINT NOT NULL,
 content_hash TEXT NOT NULL, read_at_ms BIGINT, decision TEXT, decided_at_ms BIGINT,
 PRIMARY KEY(namespace,owner,item_id)
);
CREATE TABLE IF NOT EXISTS intake_inbox_item_revisions(
 namespace TEXT NOT NULL, owner TEXT NOT NULL, item_id TEXT NOT NULL,
 source_version BIGINT NOT NULL, original_url TEXT NOT NULL, title TEXT NOT NULL,
 content TEXT NOT NULL, published_at_ms BIGINT, recorded_at_ms BIGINT NOT NULL,
 PRIMARY KEY(namespace,owner,item_id,source_version)
);
CREATE TABLE IF NOT EXISTS intake_inbox_commands(
 namespace TEXT NOT NULL, owner TEXT NOT NULL, command_key TEXT NOT NULL,
 request_hash TEXT NOT NULL, result_json TEXT NOT NULL,
 PRIMARY KEY(namespace,owner,command_key)
);
CREATE TABLE IF NOT EXISTS intake_inbox_annotations(
 namespace TEXT NOT NULL, owner TEXT NOT NULL, item_id TEXT NOT NULL,
 annotation_id TEXT NOT NULL, body TEXT NOT NULL, locator_json TEXT NOT NULL,
 source_version BIGINT NOT NULL, created_at_ms BIGINT NOT NULL,
 PRIMARY KEY(namespace,owner,annotation_id)
);
CREATE TABLE IF NOT EXISTS intake_inbox_signal_rules(
 namespace TEXT NOT NULL, owner TEXT NOT NULL, rule_id TEXT NOT NULL,
 name TEXT NOT NULL, terms_json TEXT NOT NULL, version BIGINT NOT NULL,
 created_at_ms BIGINT NOT NULL, updated_at_ms BIGINT NOT NULL,
 PRIMARY KEY(namespace,owner,rule_id), UNIQUE(namespace,owner,name)
);
"""


def _authorize(
    namespace: str, owner: str, principal_id: str, scopes: set[str], *, write=False
) -> None:
    IntakeStore._authorize(
        {"namespace": namespace, "owner": owner}, principal_id, scopes, write=write
    )


def _safe_feed_url(value: str) -> str:
    url = _text(value, "feed URL", limit=8192)
    try:
        _validate_endpoint(url, "intake-feed")
        parsed = urlsplit(url)
        if parsed.fragment or parsed.port not in (None, 443):
            raise ValueError("fragment or nonstandard port")
        _safe_url(url, {parsed.hostname.casefold()})
    except (ValueError, AttributeError, SourcePackError, ProviderError) as exc:
        raise IntakeError(
            "unsafe_feed_url", "feed URL must be public credential-free HTTPS"
        ) from exc
    return url


def _item_id(original_url: str, source_id: str) -> str:
    return "feed:" + _hash(original_url or source_id)[:32]


def _item_url(value: str, *, allow_message: bool = False) -> str:
    url = _text(value, "item URL", limit=8192)
    if allow_message and re.fullmatch(r"mid:[A-Za-z0-9._~%-]{3,1000}", url):
        return url
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise IntakeError(
            "invalid_item_url",
            "item URL must be an absolute HTTP(S) URL without credentials",
        )
    return url


def _fetch_public_feed(url: str) -> bytes:
    """Use the existing no-proxy, no-redirect bounded provider transport."""
    from src.ingestion.provider_execution import _request, _resolve, _safe_url

    host = urlsplit(url).hostname or ""
    _safe_url(url, {host.casefold()}, resolver=_resolve)
    response = _request(
        method="GET",
        url=url,
        params=None,
        body=None,
        headers={
            "User-Agent": "Noesis/1.0",
            "Accept": "application/rss+xml, application/atom+xml, application/xml",
        },
        timeout_s=15,
        max_bytes=5_000_000,
    )
    if response["status"] != 200:
        raise IntakeError(
            "feed_fetch_failed", f"feed returned HTTP {response['status']}"
        )
    return response["content"]


class IntakeInboxStore:
    """Per-user source snapshots and read/triage state, separate from mode sessions."""

    def __init__(self, conn: Any, *, initialize: bool = True, now=None):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def subscribe(
        self,
        namespace: str,
        url: str,
        name: str,
        source_kind: str,
        *,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        namespace = _text(namespace, "namespace", limit=128)
        _authorize(namespace, principal_id, principal_id, scopes, write=True)
        name = _text(name, "subscription name", limit=256)
        if source_kind not in {"rss_atom", "newsletter_feed", "newsletter_input"}:
            raise IntakeError(
                "invalid_source_kind", "source kind must be rss_atom, newsletter_feed or newsletter_input"
            )
        if source_kind == "newsletter_input":
            sender = url.removeprefix("mailto:").strip().casefold()
            if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+", sender):
                raise IntakeError("invalid_sender", "newsletter input requires a sender address")
            url = "mailto:" + sender
        else:
            url = _safe_feed_url(url)
        subscription_id = "subscription:" + _hash([namespace, principal_id, url])[:32]
        now_ms = self.now()
        self.conn.execute(
            "INSERT INTO intake_inbox_subscriptions VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(subscription_id) DO UPDATE SET name=excluded.name, "
            "source_kind=excluded.source_kind,enabled=true,updated_at_ms=excluded.updated_at_ms",
            [
                subscription_id,
                namespace,
                principal_id,
                source_kind,
                url,
                name,
                True,
                now_ms,
                now_ms,
            ],
        )
        return self._subscription(namespace, principal_id, subscription_id)

    def _subscription(
        self, namespace: str, owner: str, subscription_id: str
    ) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT subscription_id,source_kind,url,name,enabled,created_at_ms,updated_at_ms "
            "FROM intake_inbox_subscriptions WHERE namespace=? AND owner=? AND subscription_id=?",
            [namespace, owner, subscription_id],
        ).fetchone()
        if row is None:
            raise IntakeError("subscription_not_found", "subscription is unavailable")
        return dict(
            zip(
                (
                    "subscription_id",
                    "source_kind",
                    "url",
                    "name",
                    "enabled",
                    "created_at_ms",
                    "updated_at_ms",
                ),
                row,
            )
        )

    def subscriptions(
        self, namespace: str, *, principal_id: str, scopes: set[str]
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, principal_id, scopes)
        rows = self.conn.execute(
            "SELECT subscription_id FROM intake_inbox_subscriptions "
            "WHERE namespace=? AND owner=? ORDER BY subscription_id",
            [namespace, principal_id],
        ).fetchall()
        return {
            "contract": "noesis-intake-subscriptions-v1",
            "subscriptions": [
                self._subscription(namespace, principal_id, row[0]) for row in rows
            ],
        }

    def refresh(
        self,
        namespace: str,
        *,
        principal_id: str,
        scopes: set[str],
        limit_per_feed: int = 50,
        http_get=None,
    ) -> dict[str, Any]:
        """Fetch configured feeds and merge source updates into persistent inbox state."""
        _authorize(namespace, principal_id, principal_id, scopes, write=True)
        if "operator" not in scopes and "knowledge:intake:fetch" not in scopes:
            raise IntakeError(
                "unauthorized", "knowledge:intake:fetch scope is required"
            )
        if type(limit_per_feed) is not int or not 1 <= limit_per_feed <= 100:
            raise IntakeError("invalid_limit", "limit_per_feed must be 1–100")
        from src.ingestion.connectors.blog.connector import BlogConnector

        subscriptions = [
            source
            for source in self.subscriptions(
                namespace, principal_id=principal_id, scopes=scopes
            )["subscriptions"]
            if source["enabled"] and source["source_kind"] != "newsletter_input"
        ]
        if len(subscriptions) > 50:
            raise IntakeError(
                "too_many_feeds", "refresh at most 50 enabled feeds at once"
            )
        connector = BlogConnector(
            fetch_full_text=False,
            limit_per_feed=limit_per_feed,
            http_get=http_get or _fetch_public_feed,
        )
        results = []
        for source in subscriptions:
            try:
                ref = next(
                    connector.discover([{"url": source["url"], "name": source["name"]}])
                )
                docs = connector.parse(connector.fetch(ref))
                ingested = self.ingest(
                    namespace,
                    source["subscription_id"],
                    [
                        {
                            "url": doc.url or source["url"] + "#" + doc.document_id,
                            "title": doc.title or doc.document_id,
                            "content": doc.content or "",
                            "published_at_ms": doc.created_at,
                        }
                        for doc in docs
                    ],
                    principal_id=principal_id,
                    scopes=scopes,
                )
                results.append(
                    {
                        "subscription_id": source["subscription_id"],
                        "status": "ok",
                        **ingested,
                    }
                )
            except Exception as exc:  # noqa: BLE001 - keep other subscribed feeds available
                results.append(
                    {
                        "subscription_id": source["subscription_id"],
                        "status": "failed",
                        "error": {
                            "code": getattr(exc, "code", "feed_unavailable"),
                            "message": str(exc)[:200],
                        },
                    }
                )
        return {
            "contract": "noesis-intake-refresh-v1",
            "results": results,
            "feeds_checked": len(subscriptions),
            "feeds_failed": sum(result["status"] == "failed" for result in results),
        }

    def ingest(
        self,
        namespace: str,
        subscription_id: str,
        documents: list[dict[str, Any]],
        *,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        """Persist normalized feed documents without resetting user decisions."""
        _authorize(namespace, principal_id, principal_id, scopes, write=True)
        source = self._subscription(namespace, principal_id, subscription_id)
        if not source["enabled"]:
            raise IntakeError("subscription_disabled", "enable the subscription first")
        if not isinstance(documents, list) or len(documents) > 200:
            raise IntakeError("invalid_batch", "at most 200 feed items per batch")
        normalized = []
        for doc in documents:
            if not isinstance(doc, dict):
                raise IntakeError("invalid_item", "feed item must be an object")
            url = _item_url(doc.get("url"), allow_message=source["source_kind"] == "newsletter_input")
            title = _text(doc.get("title"), "item title", limit=1024)
            content = doc.get("content") or ""
            if not isinstance(content, str) or len(content) > 100_000:
                raise IntakeError(
                    "invalid_item", "item content exceeds its text budget"
                )
            published = doc.get("published_at_ms")
            if published is not None and (type(published) is not int or published < 0):
                raise IntakeError(
                    "invalid_item", "publication time must be epoch milliseconds"
                )
            normalized.append((url, title, content, published))
        counts = {"created": 0, "updated": 0, "unchanged": 0}
        now_ms = self.now()
        self.conn.execute("BEGIN")
        try:
            for url, title, content, published in normalized:
                item_id = _item_id(url, subscription_id)
                digest = _hash([url, title, content, published])
                row = self.conn.execute(
                    "SELECT source_ids_json,source_version,content_hash FROM intake_inbox_items "
                    "WHERE namespace=? AND owner=? AND item_id=?",
                    [namespace, principal_id, item_id],
                ).fetchone()
                if row is None:
                    self.conn.execute(
                        "INSERT INTO intake_inbox_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        [
                            namespace,
                            principal_id,
                            item_id,
                            _json([subscription_id]),
                            url,
                            title,
                            content,
                            published,
                            now_ms,
                            now_ms,
                            1,
                            digest,
                            None,
                            None,
                            None,
                        ],
                    )
                    self.conn.execute(
                        "INSERT INTO intake_inbox_item_revisions VALUES (?,?,?,?,?,?,?,?,?)",
                        [
                            namespace,
                            principal_id,
                            item_id,
                            1,
                            url,
                            title,
                            content,
                            published,
                            now_ms,
                        ],
                    )
                    counts["created"] += 1
                else:
                    source_ids = json.loads(row[0])
                    if subscription_id not in source_ids:
                        source_ids.append(subscription_id)
                    changed = row[2] != digest
                    self.conn.execute(
                        "UPDATE intake_inbox_items SET source_ids_json=?,original_url=?,title=?,"
                        "content=?,published_at_ms=?,last_seen_ms=?,source_version=?,content_hash=? "
                        "WHERE namespace=? AND owner=? AND item_id=?",
                        [
                            _json(source_ids),
                            url,
                            title,
                            content,
                            published,
                            now_ms,
                            row[1] + int(changed),
                            digest,
                            namespace,
                            principal_id,
                            item_id,
                        ],
                    )
                    if changed:
                        self.conn.execute(
                            "INSERT INTO intake_inbox_item_revisions VALUES (?,?,?,?,?,?,?,?,?)",
                            [
                                namespace,
                                principal_id,
                                item_id,
                                row[1] + 1,
                                url,
                                title,
                                content,
                                published,
                                now_ms,
                            ],
                        )
                    counts["updated" if changed else "unchanged"] += 1
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {
            "contract": "noesis-intake-ingest-v1",
            **counts,
            "total": len(normalized),
        }

    def ingest_newsletter_message(
        self, namespace: str, subscription_id: str, *,
        message_id: str, sender: str, subject: str, body: str,
        published_at_ms: int, principal_id: str, scopes: set[str],
    ) -> dict[str, Any]:
        """Ingest one caller-supplied message from an explicitly configured sender."""
        _authorize(namespace, principal_id, principal_id, scopes, write=True)
        source = self._subscription(namespace, principal_id, subscription_id)
        if source["source_kind"] != "newsletter_input":
            raise IntakeError("invalid_source_kind", "source is not a newsletter input")
        sender = _text(sender, "sender", limit=320).casefold()
        if source["url"] != "mailto:" + sender:
            raise IntakeError("sender_mismatch", "message sender differs from configured input")
        identity = _text(message_id, "message ID", limit=500).strip("<>")
        if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+", identity):
            raise IntakeError("invalid_message_id", "RFC message ID required")
        if type(published_at_ms) is not int or published_at_ms < 0:
            raise IntakeError("invalid_publication_time", "message publication time is required")
        subject = _text(subject, "subject", limit=1024)
        if not isinstance(body, str) or len(body) > 100_000:
            raise IntakeError("invalid_item", "message text exceeds its budget")
        locator = "mid:" + quote(identity, safe="")
        result = self.ingest(
            namespace, subscription_id,
            [{"url": locator, "title": subject, "content": body,
              "published_at_ms": published_at_ms}],
            principal_id=principal_id, scopes=scopes)
        return {**result, "source_kind": "newsletter_input",
                "authentication_state": "caller_supplied_unverified",
                "original_locator": locator}

    def _item(
        self, namespace: str, owner: str, item_id: str, revision: int | None = None
    ) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT item_id,source_ids_json,original_url,title,content,published_at_ms,"
            "first_seen_ms,last_seen_ms,source_version,read_at_ms,decision,decided_at_ms "
            "FROM intake_inbox_items WHERE namespace=? AND owner=? AND item_id=?",
            [namespace, owner, item_id],
        ).fetchone()
        if row is None:
            raise IntakeError("item_not_found", "feed item is unavailable")
        source = (row[2], row[3], row[4], row[5], row[8])
        if revision is not None:
            if type(revision) is not int or revision < 1:
                raise IntakeError(
                    "invalid_revision", "source revision must be positive"
                )
            historical = self.conn.execute(
                "SELECT original_url,title,content,published_at_ms,source_version "
                "FROM intake_inbox_item_revisions WHERE namespace=? AND owner=? AND item_id=? "
                "AND source_version=?",
                [namespace, owner, item_id, revision],
            ).fetchone()
            if historical is None:
                raise IntakeError(
                    "revision_not_found", "source revision is unavailable"
                )
            source = historical
        annotations = self._annotations(namespace, owner, item_id)
        return {
            "contract": "noesis-intake-feed-item-v1",
            "item_id": row[0],
            "source_ids": json.loads(row[1]),
            "original_url": source[0],
            "title": source[1],
            "content": source[2],
            "published_at_ms": source[3],
            "first_seen_ms": row[6],
            "last_seen_ms": row[7],
            "source_version": source[4],
            "read_at_ms": row[9],
            "decision": row[10],
            "decided_at_ms": row[11],
            "reference": {
                "kind": "intake_feed_item",
                "id": row[0],
                "namespace": namespace,
                "version": source[4],
                "locator": {"url": source[0]},
            },
            "annotations": annotations,
        }

    def _annotations(
        self, namespace: str, owner: str, item_id: str
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT annotation_id,body,locator_json,source_version,created_at_ms FROM intake_inbox_annotations "
            "WHERE namespace=? AND owner=? AND item_id=? ORDER BY created_at_ms,annotation_id",
            [namespace, owner, item_id],
        ).fetchall()
        return [
            {
                "annotation_id": row[0],
                "body": row[1],
                "locator": json.loads(row[2]),
                "source_version": row[3],
                "created_at_ms": row[4],
                "reference": {
                    "kind": "intake_annotation",
                    "id": row[0],
                    "namespace": namespace,
                    "version": 1,
                },
            }
            for row in rows
        ]

    def annotate(
        self,
        namespace: str,
        item_id: str,
        request_key: str,
        body: str,
        *,
        locator: dict[str, Any] | None = None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        """Attach an immutable, idempotent user note to the retained source item."""
        _authorize(namespace, principal_id, principal_id, scopes, write=True)
        item = self._item(namespace, principal_id, item_id)
        request_key = _text(request_key, "request_key", limit=256)
        body = _text(body, "annotation body", limit=10_000)
        locator = _bounded(locator or {}, limit=4096)
        if not isinstance(locator, dict) or set(locator) - {"start", "end", "section"}:
            raise IntakeError(
                "invalid_locator", "annotation locator has unsupported fields"
            )
        annotation_id = (
            "annotation:" + _hash([namespace, principal_id, item_id, request_key])[:32]
        )
        existing = self.conn.execute(
            "SELECT body,locator_json,source_version,created_at_ms FROM intake_inbox_annotations "
            "WHERE namespace=? AND owner=? AND annotation_id=?",
            [namespace, principal_id, annotation_id],
        ).fetchone()
        source_version = existing[2] if existing else item["source_version"]
        created_at_ms = existing[3] if existing else self.now()
        if existing:
            if existing[0] != body or json.loads(existing[1]) != locator:
                raise IntakeError(
                    "idempotency_conflict", "request_key identifies another annotation"
                )
        else:
            count = self.conn.execute(
                "SELECT count(*) FROM intake_inbox_annotations WHERE namespace=? AND owner=? AND item_id=?",
                [namespace, principal_id, item_id],
            ).fetchone()[0]
            if count >= 100:
                raise IntakeError(
                    "annotation_limit", "feed item annotation limit reached"
                )
            self.conn.execute(
                "INSERT INTO intake_inbox_annotations VALUES (?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    principal_id,
                    item_id,
                    annotation_id,
                    body,
                    _json(locator),
                    source_version,
                    created_at_ms,
                ],
            )
        return {
            "contract": "noesis-intake-annotation-v1",
            "annotation_id": annotation_id,
            "item_id": item_id,
            "body": body,
            "locator": locator,
            "source_version": source_version,
            "created_at_ms": created_at_ms,
            "reference": {
                "kind": "intake_annotation",
                "id": annotation_id,
                "namespace": namespace,
                "version": 1,
            },
            "idempotent": existing is not None,
        }

    def inspect(
        self,
        namespace: str,
        item_id: str,
        *,
        principal_id: str,
        scopes: set[str],
        revision: int | None = None,
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, principal_id, scopes)
        return self._item(namespace, principal_id, item_id, revision)

    def list(
        self,
        namespace: str,
        *,
        principal_id: str,
        scopes: set[str],
        only_unprocessed: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, principal_id, scopes)
        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or type(offset) is not int
            or offset < 0
        ):
            raise IntakeError(
                "invalid_page", "limit must be 1–100 and offset nonnegative"
            )
        rows = self.conn.execute(
            "SELECT item_id FROM intake_inbox_items WHERE namespace=? AND owner=? "
            "AND (? = false OR decision IS NULL) "
            "ORDER BY coalesce(published_at_ms,first_seen_ms) DESC,item_id LIMIT ? OFFSET ?",
            [namespace, principal_id, only_unprocessed, limit, offset],
        ).fetchall()
        remaining = self.conn.execute(
            "SELECT count(*) FROM intake_inbox_items WHERE namespace=? AND owner=? AND decision IS NULL",
            [namespace, principal_id],
        ).fetchone()[0]
        return {
            "contract": "noesis-intake-feed-page-v1",
            "items": [self._item(namespace, principal_id, row[0]) for row in rows],
            "remaining_unprocessed": remaining,
            "limit": limit,
            "offset": offset,
        }

    def signal_preview(
        self,
        namespace: str,
        terms: list[str],
        *,
        principal_id: str,
        scopes: set[str],
        only_unprocessed: bool = True,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Return explainable keyword matches without mutating inbox state."""
        _authorize(namespace, principal_id, principal_id, scopes)
        if not isinstance(terms, list) or not 1 <= len(terms) <= 20:
            raise IntakeError("invalid_filter", "provide one to 20 signal terms")
        normalized = [_text(term, "signal term", limit=80).casefold() for term in terms]
        if len(set(normalized)) != len(normalized):
            raise IntakeError("invalid_filter", "signal terms must be unique")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise IntakeError("invalid_limit", "signal limit must be 1–100")
        rows = self.conn.execute(
            "SELECT item_id,title,content,original_url,source_version FROM intake_inbox_items "
            "WHERE namespace=? AND owner=? AND (? = false OR decision IS NULL) "
            "ORDER BY coalesce(published_at_ms,first_seen_ms) DESC,item_id LIMIT 1001",
            [namespace, principal_id, only_unprocessed],
        ).fetchall()
        matches = []
        evaluated_count = 0
        for item_id, title, content, url, version in rows[:1000]:
            evaluated_count += 1
            reasons = []
            for term in normalized:
                for field, value in (("title", title), ("content", content)):
                    index = value.casefold().find(term)
                    if index >= 0:
                        reasons.append(
                            {
                                "term": term,
                                "field": field,
                                "excerpt": value[
                                    max(0, index - 40) : index + len(term) + 40
                                ],
                            }
                        )
            if reasons:
                matches.append(
                    {
                        "item_id": item_id,
                        "title": title,
                        "matched": reasons,
                        "reference": {
                            "kind": "intake_feed_item",
                            "id": item_id,
                            "namespace": namespace,
                            "version": version,
                            "locator": {"url": url},
                        },
                    }
                )
            if len(matches) >= limit:
                break
        return {
            "contract": "noesis-intake-signal-preview-v1",
            "matches": matches,
            "terms": normalized,
            "evaluated_count": evaluated_count,
            "evaluation_truncated": len(rows) > evaluated_count,
        }

    def save_signal_rule(
        self,
        namespace: str,
        name: str,
        terms: list[str],
        *,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        """Save a versioned keyword rule for repeatable, explainable triage."""
        _authorize(namespace, principal_id, principal_id, scopes, write=True)
        name = _text(name, "rule name", limit=128)
        if not isinstance(terms, list) or not 1 <= len(terms) <= 20:
            raise IntakeError("invalid_filter", "provide one to 20 signal terms")
        normalized = [_text(term, "signal term", limit=80).casefold() for term in terms]
        if len(set(normalized)) != len(normalized):
            raise IntakeError("invalid_filter", "signal terms must be unique")
        rule_id = "signal:" + _hash([namespace, principal_id, name])[:32]
        existing = self.conn.execute(
            "SELECT terms_json,version,created_at_ms FROM intake_inbox_signal_rules "
            "WHERE namespace=? AND owner=? AND rule_id=?",
            [namespace, principal_id, rule_id],
        ).fetchone()
        if existing and json.loads(existing[0]) == normalized:
            return {
                **self._signal_rule(namespace, principal_id, rule_id),
                "idempotent": True,
            }
        if not existing:
            count = self.conn.execute(
                "SELECT count(*) FROM intake_inbox_signal_rules WHERE namespace=? AND owner=?",
                [namespace, principal_id],
            ).fetchone()[0]
            if count >= 100:
                raise IntakeError(
                    "rule_limit", "at most 100 saved signal rules are allowed"
                )
        now_ms = self.now()
        self.conn.execute(
            "INSERT INTO intake_inbox_signal_rules VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(namespace,owner,rule_id) DO UPDATE SET "
            "terms_json=excluded.terms_json,version=intake_inbox_signal_rules.version+1,"
            "updated_at_ms=excluded.updated_at_ms",
            [
                namespace,
                principal_id,
                rule_id,
                name,
                _json(normalized),
                1,
                now_ms,
                now_ms,
            ],
        )
        return {
            **self._signal_rule(namespace, principal_id, rule_id),
            "idempotent": False,
        }

    def _signal_rule(self, namespace: str, owner: str, rule_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT rule_id,name,terms_json,version,created_at_ms,updated_at_ms "
            "FROM intake_inbox_signal_rules WHERE namespace=? AND owner=? AND rule_id=?",
            [namespace, owner, rule_id],
        ).fetchone()
        if row is None:
            raise IntakeError("rule_not_found", "signal rule is unavailable")
        return {
            "rule_id": row[0],
            "name": row[1],
            "terms": json.loads(row[2]),
            "version": row[3],
            "created_at_ms": row[4],
            "updated_at_ms": row[5],
        }

    def signal_rules(
        self, namespace: str, *, principal_id: str, scopes: set[str]
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, principal_id, scopes)
        rows = self.conn.execute(
            "SELECT rule_id FROM intake_inbox_signal_rules WHERE namespace=? AND owner=? ORDER BY name",
            [namespace, principal_id],
        ).fetchall()
        return {
            "contract": "noesis-intake-signal-rules-v1",
            "rules": [
                self._signal_rule(namespace, principal_id, row[0]) for row in rows
            ],
        }

    def preview_signal_rule(
        self,
        namespace: str,
        rule_id: str,
        *,
        principal_id: str,
        scopes: set[str],
        only_unprocessed: bool = True,
        limit: int = 50,
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, principal_id, scopes)
        rule = self._signal_rule(namespace, principal_id, rule_id)
        return {
            **self.signal_preview(
                namespace,
                rule["terms"],
                principal_id=principal_id,
                scopes=scopes,
                only_unprocessed=only_unprocessed,
                limit=limit,
            ),
            "rule": rule,
        }

    def start_awareness(
        self,
        namespace: str,
        request_key: str,
        *,
        intent: str,
        principal_id: str,
        scopes: set[str],
        duration_minutes: int = 15,
        workspace_links: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Snapshot the unprocessed inbox queue into a bounded Awareness session."""
        _authorize(namespace, principal_id, principal_id, scopes, write=True)
        store = IntakeStore(self.conn)
        replay_id = "intake:" + _hash([namespace, principal_id, request_key])[:32]
        try:
            existing = store.inspect(
                namespace, replay_id, principal_id=principal_id, scopes=scopes
            )
        except IntakeError as exc:
            if exc.code != "session_not_found":
                raise
        else:
            if (
                existing["mode"] != "Awareness"
                or existing["intent"] != intent
                or existing["duration_minutes"] != duration_minutes
                or (
                    workspace_links is not None
                    and existing["workspace_links"] != workspace_links
                )
            ):
                raise IntakeError(
                    "idempotency_conflict", "request_key identifies another session"
                )
            return {**existing, "idempotent": True}
        rows = self.conn.execute(
            "SELECT item_id FROM intake_inbox_items WHERE namespace=? AND owner=? "
            "AND decision IS NULL ORDER BY coalesce(published_at_ms,first_seen_ms) DESC,item_id LIMIT 1001",
            [namespace, principal_id],
        ).fetchall()
        if not rows:
            raise IntakeError("empty_inbox", "no unprocessed feed items are available")
        if len(rows) > 1000:
            raise IntakeError(
                "queue_too_large",
                "triage or filter the inbox before starting a session",
            )
        return store.create(
            namespace,
            "Awareness",
            request_key,
            intent=intent,
            inputs={"feed_item_ids": [row[0] for row in rows]},
            duration_minutes=duration_minutes,
            workspace_links=workspace_links,
            principal_id=principal_id,
            scopes=scopes,
        )

    def triage_awareness(
        self,
        namespace: str,
        session_id: str,
        item_id: str,
        command_key: str,
        *,
        expected_revision: int,
        decision: str,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        """Commit an inbox decision and session revision in one transaction."""
        return self.triage_awareness_batch(
            namespace,
            session_id,
            {item_id: decision},
            command_key,
            expected_revision=expected_revision,
            principal_id=principal_id,
            scopes=scopes,
        )

    def triage_awareness_batch(
        self,
        namespace: str,
        session_id: str,
        decisions: dict[str, str],
        command_key: str,
        *,
        expected_revision: int,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        """Commit a bounded batch of decisions and one session revision atomically."""
        if (
            not isinstance(decisions, dict)
            or not 1 <= len(decisions) <= 100
            or any(
                not isinstance(item_id, str) or not item_id or decision not in DECISIONS
                for item_id, decision in decisions.items()
            )
        ):
            raise IntakeError("invalid_decision", "provide 1–100 queued item decisions")

        def update_inbox(state: dict[str, Any], _payload: dict[str, Any]) -> None:
            if state["mode"] != "Awareness" or set(decisions) - set(
                state["inputs"]["feed_item_ids"]
            ):
                raise IntakeError(
                    "item_not_in_queue", "item is outside this Awareness queue"
                )
            now_ms = self.now()
            for item_id, decision in decisions.items():
                self._item(namespace, principal_id, item_id)
                self.conn.execute(
                    "UPDATE intake_inbox_items SET decision=?,decided_at_ms=? "
                    "WHERE namespace=? AND owner=? AND item_id=?",
                    [decision, now_ms, namespace, principal_id, item_id],
                )

        return IntakeStore(self.conn).command(
            namespace,
            session_id,
            command_key,
            expected_revision=expected_revision,
            action="record",
            payload={"data": {"decisions": decisions}},
            principal_id=principal_id,
            scopes=scopes,
            record_hook=update_inbox,
        )

    def promote_awareness_item(
        self,
        namespace: str,
        awareness_session_id: str,
        item_id: str,
        request_key: str,
        *,
        target_mode: str,
        reason: str,
        intent: str,
        workspace_links: list[dict[str, Any]] | None = None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        """Create a linked mode with exact source and annotation identities."""
        _authorize(namespace, principal_id, principal_id, scopes, write=True)
        if target_mode not in {"Exploration", "Deep Research", "Problem-Solving"}:
            raise IntakeError(
                "invalid_mode",
                "feed escalation supports Exploration, Deep Research, or Problem-Solving",
            )
        request_key = _text(request_key, "request_key", limit=256)
        reason = _text(reason, "transition reason")
        intent = _text(intent, "intent")
        store = IntakeStore(self.conn)
        replay_id = "intake:" + _hash([namespace, principal_id, request_key])[:32]
        try:
            existing = store.inspect(
                namespace, replay_id, principal_id=principal_id, scopes=scopes
            )
        except IntakeError as exc:
            if exc.code != "session_not_found":
                raise
        else:
            if (
                existing["mode"] != target_mode
                or existing["origin"] is None
                or existing["origin"]["session_id"] != awareness_session_id
                or existing["origin"]["reason"] != reason
                or existing["intent"] != intent
                or existing["inputs"].get("feed_item_id") != item_id
                or (
                    workspace_links is not None
                    and existing["workspace_links"] != workspace_links
                )
            ):
                raise IntakeError(
                    "idempotency_conflict", "request_key identifies another transition"
                )
            return {**existing, "idempotent": True}
        source_session = store.inspect(
            namespace, awareness_session_id, principal_id=principal_id, scopes=scopes
        )
        if (
            source_session["mode"] != "Awareness"
            or item_id not in source_session["inputs"]["feed_item_ids"]
        ):
            raise IntakeError(
                "item_not_in_queue", "item is outside this Awareness queue"
            )
        item = self._item(namespace, principal_id, item_id)
        if (
            item["decision"] != "escalate"
            or source_session["data"].get("decisions", {}).get(item_id) != "escalate"
        ):
            raise IntakeError(
                "not_escalated", "triage the item as escalate before promotion"
            )
        references = [item["reference"]] + [
            annotation["reference"] for annotation in item["annotations"]
        ]
        return store.create(
            namespace,
            target_mode,
            request_key,
            intent=intent,
            inputs={"feed_item_id": item_id},
            origin={"session_id": awareness_session_id, "reason": reason},
            references=references,
            workspace_links=workspace_links,
            principal_id=principal_id,
            scopes=scopes,
        )

    def mark_read(
        self,
        namespace: str,
        item_id: str,
        command_key: str,
        *,
        read: bool,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        return self._command(
            namespace,
            item_id,
            command_key,
            "read",
            {"read": read},
            principal_id=principal_id,
            scopes=scopes,
        )

    def decide(
        self,
        namespace: str,
        item_id: str,
        command_key: str,
        *,
        decision: str,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        if decision not in DECISIONS:
            raise IntakeError(
                "invalid_decision",
                "choose watch, escalate, schedule, discard, archive, or flag",
            )
        return self._command(
            namespace,
            item_id,
            command_key,
            "decide",
            {"decision": decision},
            principal_id=principal_id,
            scopes=scopes,
        )

    def _command(
        self,
        namespace: str,
        item_id: str,
        command_key: str,
        action: str,
        payload: dict[str, Any],
        *,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, principal_id, scopes, write=True)
        command_key = _text(command_key, "command_key", limit=256)
        digest = _hash([item_id, action, payload])
        self.conn.execute("BEGIN")
        try:
            existing = self.conn.execute(
                "SELECT request_hash,result_json FROM intake_inbox_commands "
                "WHERE namespace=? AND owner=? AND command_key=?",
                [namespace, principal_id, command_key],
            ).fetchone()
            if existing:
                if existing[0] != digest:
                    raise IntakeError(
                        "idempotency_conflict",
                        "command_key identifies another operation",
                    )
                self.conn.execute("COMMIT")
                return {**json.loads(existing[1]), "idempotent": True}
            self._item(namespace, principal_id, item_id)
            if action == "read":
                self.conn.execute(
                    "UPDATE intake_inbox_items SET read_at_ms=? WHERE namespace=? AND owner=? AND item_id=?",
                    [
                        self.now() if payload["read"] else None,
                        namespace,
                        principal_id,
                        item_id,
                    ],
                )
            else:
                self.conn.execute(
                    "UPDATE intake_inbox_items SET decision=?,decided_at_ms=? "
                    "WHERE namespace=? AND owner=? AND item_id=?",
                    [payload["decision"], self.now(), namespace, principal_id, item_id],
                )
            result = self._item(namespace, principal_id, item_id)
            self.conn.execute(
                "INSERT INTO intake_inbox_commands VALUES (?,?,?,?,?)",
                [namespace, principal_id, command_key, digest, _json(result)],
            )
            self.conn.execute("COMMIT")
            return {**result, "idempotent": False}
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
