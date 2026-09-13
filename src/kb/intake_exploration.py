"""Lightweight, owner-scoped Exploration captures linked to durable mode sessions."""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlsplit

import duckdb

from src.ingestion.provider_execution import (
    ProviderError,
    _request,
    _resolve,
    _safe_url,
)
from src.ingestion.source_packs import SourcePackError, _validate_endpoint
from src.kb.intake_inbox import IntakeInboxStore
from src.kb.intake_modes import IntakeError, IntakeStore, _bounded, _hash, _json, _text

_DDL = """
CREATE TABLE IF NOT EXISTS intake_exploration_sources(
 namespace TEXT NOT NULL, owner TEXT NOT NULL, source_id TEXT NOT NULL,
 url TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL,
 acquisition TEXT NOT NULL, extractor TEXT, content_hash TEXT NOT NULL,
 version BIGINT NOT NULL, first_seen_ms BIGINT NOT NULL, last_seen_ms BIGINT NOT NULL,
 PRIMARY KEY(namespace,owner,source_id)
);
CREATE TABLE IF NOT EXISTS intake_exploration_source_revisions(
 namespace TEXT NOT NULL, owner TEXT NOT NULL, source_id TEXT NOT NULL,
 version BIGINT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL,
 content TEXT NOT NULL, acquisition TEXT NOT NULL, extractor TEXT,
 recorded_at_ms BIGINT NOT NULL,
 PRIMARY KEY(namespace,owner,source_id,version)
);
CREATE TABLE IF NOT EXISTS intake_exploration_annotations(
 namespace TEXT NOT NULL, owner TEXT NOT NULL, source_id TEXT NOT NULL,
 annotation_id TEXT NOT NULL, source_version BIGINT NOT NULL,
 body TEXT NOT NULL, locator_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
 PRIMARY KEY(namespace,owner,annotation_id)
);
"""


def _capture_url(value: str) -> str:
    url = _text(value, "source URL", limit=8192)
    parsed = urlsplit(url)
    try:
        _validate_endpoint(url, "exploration-source")
        _safe_url(url, {(parsed.hostname or "").casefold()})
    except (ProviderError, SourcePackError) as exc:
        raise IntakeError(
            "unsafe_source_url", "source URL must be public credential-free HTTPS"
        ) from exc
    return url


def _fetch_readable_page(url: str) -> tuple[str, str | None, str | None]:
    """Acquire one public page with the bounded no-proxy, no-redirect transport."""
    from src.ingestion.extract import extract_article

    host = urlsplit(url).hostname or ""
    _safe_url(url, {host.casefold()}, resolver=_resolve)
    response = _request(
        method="GET",
        url=url,
        params=None,
        body=None,
        headers={"User-Agent": "Noesis/1.0", "Accept": "text/html, text/plain"},
        timeout_s=15,
        max_bytes=2_000_000,
    )
    if response["status"] != 200:
        raise IntakeError(
            "source_fetch_failed", f"source returned HTTP {response['status']}"
        )
    content_type = response["headers"].get("content-type", "").split(";", 1)[0].lower()
    if content_type not in {"text/html", "text/plain"}:
        raise IntakeError(
            "unsupported_content_type", "only HTML and plain text can be captured"
        )
    if content_type == "text/plain":
        return (
            response["content"].decode("utf-8", errors="replace")[:100_000],
            None,
            "plain-text",
        )
    result = extract_article(response["content"], url=url)
    if result is None:
        return "", None, None
    return result.text[:100_000], result.title, result.method


class IntakeExplorationStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def capture(
        self,
        namespace: str,
        session_id: str,
        command_key: str,
        *,
        expected_revision: int,
        url: str,
        title: str,
        note: str = "",
        saved: bool = False,
        content: str | None = None,
        fetch_readable: bool = False,
        principal_id: str,
        scopes: set[str],
        page_fetch=None,
    ) -> dict[str, Any]:
        """Add a visited/saved source with a versioned snapshot and optional user note."""
        url = _capture_url(url)
        title = _text(title, "source title", limit=1024)
        if not isinstance(note, str) or len(note) > 10_000:
            raise IntakeError("invalid_note", "note exceeds its text budget")
        if type(saved) is not bool or type(fetch_readable) is not bool:
            raise IntakeError(
                "invalid_input", "saved and fetch_readable must be booleans"
            )
        if content is not None and (
            not isinstance(content, str) or len(content) > 100_000
        ):
            raise IntakeError(
                "invalid_content", "supplied content exceeds its text budget"
            )
        if fetch_readable and content is not None:
            raise IntakeError(
                "invalid_input", "choose fetched or caller-supplied content"
            )
        if (
            fetch_readable
            and "operator" not in scopes
            and "knowledge:intake:fetch" not in scopes
        ):
            raise IntakeError(
                "unauthorized", "knowledge:intake:fetch scope is required"
            )
        store = IntakeStore(self.conn)
        state = store.inspect(
            namespace, session_id, principal_id=principal_id, scopes=scopes
        )
        if state["mode"] != "Exploration":
            raise IntakeError("invalid_mode", "capture requires an Exploration session")
        payload = {
            "data": {
                "capture_request": {
                    "url": url,
                    "title": title,
                    "note": note,
                    "saved": saved,
                    "content_sha256": _hash(content) if content is not None else None,
                    "fetch_readable": fetch_readable,
                }
            }
        }
        # Avoid a network retry for a command already durably committed.
        prior = self.conn.execute(
            "SELECT revision FROM intake_session_commands WHERE session_id=? AND command_key=?",
            [session_id, command_key],
        ).fetchone()
        if prior:
            return store.command(
                namespace,
                session_id,
                command_key,
                expected_revision=expected_revision,
                action="record",
                payload=payload,
                principal_id=principal_id,
                scopes=scopes,
            )
        if state["status"] != "active":
            raise IntakeError("inactive_session", "resume Exploration before capturing")
        if fetch_readable:
            readable, extracted_title, extractor = (page_fetch or _fetch_readable_page)(
                url
            )
            if extracted_title and title == url:
                title = extracted_title[:1024]
            content = readable
            acquisition = "web_fetch"
        else:
            extractor = None
            acquisition = "caller_supplied" if content is not None else "url_only"
        content = content or ""
        source_id = "explore:" + _hash([namespace, principal_id, url])[:32]
        item = {
            "source_id": source_id,
            "url": url,
            "title": title,
            "note": note,
            "saved": saved,
            "acquisition": acquisition,
        }
        ref = {
            "kind": "exploration_source",
            "id": source_id,
            "namespace": namespace,
            "version": 1,
            "locator": {"url": url},
        }

        def save_snapshot(updated: dict[str, Any], _payload: dict[str, Any]) -> None:
            if updated["mode"] != "Exploration":
                raise IntakeError(
                    "invalid_mode", "capture requires an Exploration session"
                )
            trail = updated["data"].get("trail", [])
            if len(trail) >= 1000:
                raise IntakeError(
                    "trail_limit", "Exploration trail exceeds 1000 visits"
                )
            now_ms = self.now()
            digest = _hash([url, title, content, acquisition, extractor])
            row = self.conn.execute(
                "SELECT version,content_hash,content FROM intake_exploration_sources "
                "WHERE namespace=? AND owner=? AND source_id=?",
                [namespace, principal_id, source_id],
            ).fetchone()
            preserve = row is not None and bool(row[2]) and not content
            if preserve:
                digest = row[1]
                item["snapshot_preserved"] = True
            version = 1 if row is None else row[0] + int(row[1] != digest)
            if row is None:
                self.conn.execute(
                    "INSERT INTO intake_exploration_sources VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        namespace,
                        principal_id,
                        source_id,
                        url,
                        title,
                        content,
                        acquisition,
                        extractor,
                        digest,
                        version,
                        now_ms,
                        now_ms,
                    ],
                )
            elif preserve:
                self.conn.execute(
                    "UPDATE intake_exploration_sources SET last_seen_ms=? "
                    "WHERE namespace=? AND owner=? AND source_id=?",
                    [now_ms, namespace, principal_id, source_id],
                )
            else:
                self.conn.execute(
                    "UPDATE intake_exploration_sources SET title=?,content=?,acquisition=?,"
                    "extractor=?,content_hash=?,version=?,last_seen_ms=? "
                    "WHERE namespace=? AND owner=? AND source_id=?",
                    [
                        title,
                        content,
                        acquisition,
                        extractor,
                        digest,
                        version,
                        now_ms,
                        namespace,
                        principal_id,
                        source_id,
                    ],
                )
            if row is None or row[1] != digest:
                self.conn.execute(
                    "INSERT INTO intake_exploration_source_revisions VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [
                        namespace,
                        principal_id,
                        source_id,
                        version,
                        url,
                        title,
                        content,
                        acquisition,
                        extractor,
                        now_ms,
                    ],
                )
            ref["version"] = version
            item["version"] = version
            item["visited_at_ms"] = now_ms
            item["visit_id"] = "visit:" + _hash([session_id, command_key])[:32]
            updated["data"]["trail"] = [*trail, item]
            if ref not in updated["references"]:
                updated["references"].append(ref)

        return store.command(
            namespace,
            session_id,
            command_key,
            expected_revision=expected_revision,
            action="record",
            payload=payload,
            principal_id=principal_id,
            scopes=scopes,
            record_hook=save_snapshot,
        )

    def inspect_source(
        self,
        namespace: str,
        source_id: str,
        *,
        principal_id: str,
        scopes: set[str],
        version: int | None = None,
    ) -> dict[str, Any]:
        IntakeStore._authorize(
            {"namespace": namespace, "owner": principal_id},
            principal_id,
            scopes,
        )
        row = self.conn.execute(
            "SELECT version,url,title,content,acquisition,extractor,first_seen_ms,last_seen_ms "
            "FROM intake_exploration_sources WHERE namespace=? AND owner=? AND source_id=?",
            [namespace, principal_id, source_id],
        ).fetchone()
        if row is None:
            raise IntakeError("source_not_found", "Exploration source is unavailable")
        if version is not None:
            if type(version) is not int or version < 1:
                raise IntakeError("invalid_revision", "source version must be positive")
            historical = self.conn.execute(
                "SELECT version,url,title,content,acquisition,extractor,recorded_at_ms "
                "FROM intake_exploration_source_revisions WHERE namespace=? AND owner=? "
                "AND source_id=? AND version=?",
                [namespace, principal_id, source_id, version],
            ).fetchone()
            if historical is None:
                raise IntakeError("revision_not_found", "source version is unavailable")
            row = (*historical[:6], row[6], historical[6])
        annotations = self._annotations(namespace, principal_id, source_id)
        return {
            "contract": "noesis-exploration-source-v1",
            "source_id": source_id,
            "version": row[0],
            "url": row[1],
            "title": row[2],
            "content": row[3],
            "acquisition": row[4],
            "extractor": row[5],
            "first_seen_ms": row[6],
            "last_seen_ms": row[7],
            "annotations": [
                annotation for annotation in annotations
                if annotation["source_version"] == row[0]
            ],
            "reference": {
                "kind": "exploration_source",
                "id": source_id,
                "namespace": namespace,
                "version": row[0],
                "locator": {"url": row[1]},
            },
        }

    def _annotations(
        self, namespace: str, owner: str, source_id: str
    ) -> list[dict[str, Any]]:
        try:
            rows = self.conn.execute(
                "SELECT annotation_id,source_version,body,locator_json,created_at_ms "
                "FROM intake_exploration_annotations WHERE namespace=? AND owner=? "
                "AND source_id=? ORDER BY created_at_ms,annotation_id",
                [namespace, owner, source_id],
            ).fetchall()
        except duckdb.CatalogException as exc:
            if "intake_exploration_annotations" not in str(exc):
                raise
            # Existing read-only stores gain this table on their first write.
            return []
        return [
            {
                "annotation_id": row[0],
                "source_id": source_id,
                "source_version": row[1],
                "body": row[2],
                "locator": json.loads(row[3]),
                "created_at_ms": row[4],
                "reference": {
                    "kind": "exploration_annotation",
                    "id": row[0],
                    "namespace": namespace,
                    "version": 1,
                },
            }
            for row in rows
        ]

    def annotate_source(
        self,
        namespace: str,
        source_id: str,
        request_key: str,
        body: str,
        *,
        locator: dict[str, Any] | None = None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        """Attach an immutable note to the current authoritative source version."""
        IntakeStore._authorize(
            {"namespace": namespace, "owner": principal_id},
            principal_id,
            scopes,
            write=True,
        )
        request_key = _text(request_key, "request_key", limit=256)
        body = _text(body, "annotation body", limit=10_000)
        locator = _bounded(locator or {}, limit=4096)
        if not isinstance(locator, dict) or set(locator) - {"start", "end", "section"}:
            raise IntakeError("invalid_locator", "annotation locator has unsupported fields")
        source = self.inspect_source(
            namespace, source_id, principal_id=principal_id, scopes=scopes
        )
        annotation_id = "explore-note:" + _hash(
            [namespace, principal_id, source_id, request_key]
        )[:32]
        existing = self.conn.execute(
            "SELECT source_version,body,locator_json FROM intake_exploration_annotations "
            "WHERE namespace=? AND owner=? AND annotation_id=?",
            [namespace, principal_id, annotation_id],
        ).fetchone()
        if existing:
            if existing[1] != body or json.loads(existing[2]) != locator:
                raise IntakeError(
                    "idempotency_conflict", "request_key identifies another annotation"
                )
        else:
            count = self.conn.execute(
                "SELECT count(*) FROM intake_exploration_annotations "
                "WHERE namespace=? AND owner=? AND source_id=?",
                [namespace, principal_id, source_id],
            ).fetchone()[0]
            if count >= 100:
                raise IntakeError("annotation_limit", "source annotation limit reached")
            self.conn.execute(
                "INSERT INTO intake_exploration_annotations VALUES (?,?,?,?,?,?,?,?)",
                [namespace, principal_id, source_id, annotation_id, source["version"],
                 body, _json(locator), self.now()],
            )
        note = next(
            annotation for annotation in self._annotations(namespace, principal_id, source_id)
            if annotation["annotation_id"] == annotation_id
        )
        return {"contract": "noesis-exploration-annotation-v1", **note,
                "idempotent": existing is not None}

    def link_feed_item(
        self,
        namespace: str,
        session_id: str,
        item_id: str,
        command_key: str,
        *,
        expected_revision: int,
        note: str = "",
        saved: bool = False,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        """Visit a feed source without recapturing or changing its authoritative ID."""
        if not isinstance(note, str) or len(note) > 10_000 or type(saved) is not bool:
            raise IntakeError(
                "invalid_input", "invalid Exploration visit note or saved state"
            )
        store = IntakeStore(self.conn)
        state = store.inspect(
            namespace, session_id, principal_id=principal_id, scopes=scopes
        )
        if state["mode"] != "Exploration":
            raise IntakeError("invalid_mode", "feed visit requires Exploration")
        payload = {
            "data": {
                "feed_visit_request": {
                    "item_id": item_id,
                    "note": note,
                    "saved": saved,
                }
            }
        }
        prior = self.conn.execute(
            "SELECT revision FROM intake_session_commands WHERE session_id=? AND command_key=?",
            [session_id, command_key],
        ).fetchone()
        if prior:
            return store.command(
                namespace,
                session_id,
                command_key,
                expected_revision=expected_revision,
                action="record",
                payload=payload,
                principal_id=principal_id,
                scopes=scopes,
            )
        item = IntakeInboxStore(self.conn, initialize=False).inspect(
            namespace,
            item_id,
            principal_id=principal_id,
            scopes=scopes,
        )

        def append_visit(updated: dict[str, Any], _payload: dict[str, Any]) -> None:
            trail = updated["data"].get("trail", [])
            if len(trail) >= 1000:
                raise IntakeError(
                    "trail_limit", "Exploration trail exceeds 1000 visits"
                )
            updated["data"]["trail"] = [
                *trail,
                {
                    "visit_id": "visit:" + _hash([session_id, command_key])[:32],
                    "source_id": item_id,
                    "url": item["original_url"],
                    "title": item["title"],
                    "note": note,
                    "saved": saved,
                    "version": item["source_version"],
                    "visited_at_ms": self.now(),
                },
            ]
            if item["reference"] not in updated["references"]:
                updated["references"].append(item["reference"])

        return store.command(
            namespace,
            session_id,
            command_key,
            expected_revision=expected_revision,
            action="record",
            payload=payload,
            principal_id=principal_id,
            scopes=scopes,
            record_hook=append_visit,
        )
