"""Persistent bounded synchronization loop for a local Noesis workspace."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SYNC_CONTRACT = "noesis-sync-v1"
DEFAULT_INTERVAL_SECONDS = 300.0
MAX_BACKOFF_SECONDS = 1800.0
MAX_ITEMS_PER_PASS = 1000

_DDL = """
CREATE SEQUENCE IF NOT EXISTS noesis_sync_run_sequence START 1;
CREATE TABLE IF NOT EXISTS noesis_sync_runs(
  run_id TEXT PRIMARY KEY,
  sequence BIGINT NOT NULL UNIQUE,
  started_at_ms BIGINT NOT NULL,
  completed_at_ms BIGINT,
  status TEXT NOT NULL,
  receipt_json TEXT,
  error_json TEXT
);
CREATE TABLE IF NOT EXISTS noesis_sync_feed_state(
  namespace TEXT NOT NULL,
  owner TEXT NOT NULL,
  item_id TEXT NOT NULL,
  source_version BIGINT NOT NULL,
  document_id TEXT,
  status TEXT NOT NULL,
  error_code TEXT,
  synced_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace,owner,item_id)
);
CREATE TABLE IF NOT EXISTS noesis_sync_state(
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at_ms BIGINT NOT NULL
);
"""


class SyncError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SyncLock:
    """Advisory process lock held for one sync pass or an entire daemon lifetime."""

    def __init__(self, root: Path) -> None:
        self.path = root / "sync.lock"
        self.handle = None

    def __enter__(self):
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        os.chmod(self.path, 0o600)
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.handle.close()
            self.handle = None
            raise SyncError(
                "sync_already_running",
                f"another Noesis sync process holds {self.path}",
            ) from exc
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(
            json.dumps({"pid": os.getpid(), "acquired_at_ms": _now_ms()}) + "\n"
        )
        self.handle.flush()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.handle is None:
            return
        import fcntl

        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def _now_ms() -> int:
    return int(time.time() * 1000)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _safe_error(exc: BaseException) -> dict[str, str]:
    code = str(getattr(exc, "code", type(exc).__name__) or "sync_failed")
    return {"code": code[:120], "message": str(exc)[:500]}


def _table_exists(conn: Any, name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='main' AND table_name=?",
            [name],
        ).fetchone()
    )


class SyncStore:
    def __init__(self, conn: Any) -> None:
        self.conn = conn
        conn.execute(_DDL)

    def start(self) -> dict[str, Any]:
        started = _now_ms()
        interrupted = int(
            self.conn.execute(
                "SELECT count(*) FROM noesis_sync_runs WHERE status='running'"
            ).fetchone()[0]
        )
        if interrupted:
            self.conn.execute(
                "UPDATE noesis_sync_runs SET completed_at_ms=?,status='interrupted',"
                "error_json=? WHERE status='running'",
                [
                    started,
                    _canonical(
                        {
                            "code": "process_interrupted",
                            "message": "previous sync process ended before completion",
                        }
                    ),
                ],
            )
        sequence = int(
            self.conn.execute(
                "SELECT nextval('noesis_sync_run_sequence')"
            ).fetchone()[0]
        )
        run_id = f"sync:{sequence}"
        self.conn.execute(
            "INSERT INTO noesis_sync_runs VALUES (?,?,?,NULL,'running',NULL,NULL)",
            [run_id, sequence, started],
        )
        return {
            "run_id": run_id,
            "sequence": sequence,
            "started_at_ms": started,
            "recovered_interrupted_runs": interrupted,
        }

    def finish(self, run_id: str, status: str, receipt: dict[str, Any]) -> None:
        completed = int(receipt["completed_at_ms"])
        error = receipt.get("error")
        self.conn.execute(
            "UPDATE noesis_sync_runs SET completed_at_ms=?,status=?,receipt_json=?,"
            "error_json=? WHERE run_id=?",
            [
                completed,
                status,
                _canonical(receipt),
                None if error is None else _canonical(error),
                run_id,
            ],
        )
        if status == "complete":
            self.conn.execute(
                "INSERT INTO noesis_sync_state VALUES ('last_success',?,?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,"
                "updated_at_ms=excluded.updated_at_ms",
                [_canonical({"run_id": run_id, "completed_at_ms": completed}), completed],
            )

    def candidates(
        self, namespace: str, owner: str, limit: int
    ) -> list[dict[str, Any]]:
        if not _table_exists(self.conn, "intake_inbox_items"):
            return []
        rows = self.conn.execute(
            "SELECT i.item_id,i.source_ids_json,i.original_url,i.title,i.content,"
            "i.published_at_ms,i.last_seen_ms,i.source_version "
            "FROM intake_inbox_items i LEFT JOIN noesis_sync_feed_state s "
            "ON s.namespace=i.namespace AND s.owner=i.owner AND s.item_id=i.item_id "
            "WHERE i.namespace=? AND i.owner=? "
            "AND (s.item_id IS NULL OR s.source_version<i.source_version) "
            "ORDER BY coalesce(i.published_at_ms,i.first_seen_ms),i.item_id LIMIT ?",
            [namespace, owner, int(limit)],
        ).fetchall()
        return [
            {
                "item_id": row[0],
                "source_ids": json.loads(row[1]),
                "url": row[2],
                "title": row[3],
                "content": row[4],
                "published_at_ms": row[5],
                "last_seen_ms": row[6],
                "source_version": int(row[7]),
            }
            for row in rows
        ]

    def mark(
        self,
        namespace: str,
        owner: str,
        item: dict[str, Any],
        *,
        status: str,
        document_id: str | None,
        error_code: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO noesis_sync_feed_state VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(namespace,owner,item_id) DO UPDATE SET "
            "source_version=excluded.source_version,document_id=excluded.document_id,"
            "status=excluded.status,error_code=excluded.error_code,"
            "synced_at_ms=excluded.synced_at_ms",
            [
                namespace,
                owner,
                item["item_id"],
                item["source_version"],
                document_id,
                status,
                error_code,
                _now_ms(),
            ],
        )

    def backlog(self, owner: str) -> int:
        if not _table_exists(self.conn, "intake_inbox_items"):
            return 0
        return int(
            self.conn.execute(
                "SELECT count(*) FROM intake_inbox_items i "
                "LEFT JOIN noesis_sync_feed_state s "
                "ON s.namespace=i.namespace AND s.owner=i.owner AND s.item_id=i.item_id "
                "WHERE i.owner=? AND (s.item_id IS NULL OR s.source_version<i.source_version)",
                [owner],
            ).fetchone()[0]
        )


def _dry_run(config, conn: Any, max_items: int) -> dict[str, Any]:
    from src.kb.registry import load_registry

    registry = load_registry(config.domains)
    subscriptions = 0
    unsynced = 0
    if _table_exists(conn, "intake_inbox_subscriptions"):
        subscriptions = int(
            conn.execute(
                "SELECT count(*) FROM intake_inbox_subscriptions "
                "WHERE owner=? AND enabled=true",
                [config.principal],
            ).fetchone()[0]
        )
    if _table_exists(conn, "intake_inbox_items"):
        if _table_exists(conn, "noesis_sync_feed_state"):
            unsynced = int(
                conn.execute(
                    "SELECT count(*) FROM intake_inbox_items i "
                    "LEFT JOIN noesis_sync_feed_state s "
                    "ON s.namespace=i.namespace AND s.owner=i.owner AND s.item_id=i.item_id "
                    "WHERE i.owner=? AND (s.item_id IS NULL OR s.source_version<i.source_version)",
                    [config.principal],
                ).fetchone()[0]
            )
        else:
            unsynced = int(
                conn.execute(
                    "SELECT count(*) FROM intake_inbox_items WHERE owner=?",
                    [config.principal],
                ).fetchone()[0]
            )
    watches = 0
    if _table_exists(conn, "claim_watches"):
        watches = int(
            conn.execute(
                "SELECT count(*) FROM claim_watches WHERE principal_id=? AND status='active'",
                [config.principal],
            ).fetchone()[0]
        )
    return {
        "contract": SYNC_CONTRACT,
        "status": "planned",
        "dry_run": True,
        "started_at_ms": _now_ms(),
        "completed_at_ms": _now_ms(),
        "plan": {
            "domains": [d.name for d in registry.domains()],
            "enabled_subscriptions": subscriptions,
            "unsynced_feed_items": unsynced,
            "active_watches": watches,
            "max_items": max_items,
            "network": "explicit-subscriptions-only",
            "research_execution": False,
            "paid_acquisition": False,
        },
    }


def _feed_document(item: dict[str, Any], namespace: str):
    from services.ingest.common.document_model import Document

    host = urlsplit(item["url"]).hostname or None
    source_ids = item.get("source_ids") or []
    return Document(
        document_id=item["item_id"],
        source_type="blog",
        language="en",
        ingested_at=int(item["last_seen_ms"] or _now_ms()),
        source_id=host or (source_ids[0] if source_ids else None),
        url=item["url"],
        title=item["title"],
        content=item["content"],
        created_at=item.get("published_at_ms"),
        metadata={
            "intake_namespace": namespace,
            "intake_source_ids": source_ids,
            "intake_source_version": item["source_version"],
            "sync_managed": True,
        },
    )


def _next_watch_watermark(conn: Any) -> int:
    from src.kb.watches import ensure_watch_schema

    ensure_watch_schema(conn)
    row = conn.execute(
        "SELECT COALESCE(MAX(watermark),0) FROM claim_watch_watermarks"
    ).fetchone()
    return int(row[0]) + 1


def _sync_pass(config, *, max_items: int = 200, dry_run: bool = False) -> dict[str, Any]:
    from src.kb.registry import load_registry
    from src.noesis_cli.config import open_warehouse

    if not 1 <= int(max_items) <= MAX_ITEMS_PER_PASS:
        raise SyncError("invalid_limit", "max_items must be between 1 and 1000")
    conn = open_warehouse(config)
    if dry_run:
        try:
            return _dry_run(config, conn, int(max_items))
        finally:
            conn.close()

    store = SyncStore(conn)
    run = store.start()
    receipt: dict[str, Any] = {
        "contract": SYNC_CONTRACT,
        **run,
        "dry_run": False,
        "status": "running",
        "stages": {},
    }
    errors: list[dict[str, Any]] = []
    try:
        from src.gateway import ingest_documents
        from src.kb.intake_inbox import IntakeInboxStore
        from src.kb.maintenance import MaintenanceOrchestrator
        from src.kb.membership import run_membership_pass
        from src.kb.watches import commit_watch_watermark, run_watch_matcher

        registry = load_registry(config.domains)
        domains = [definition.name for definition in registry.domains()]
        inbox = IntakeInboxStore(conn)
        refresh_rows = []
        for namespace in domains:
            try:
                listed = inbox.subscriptions(
                    namespace, principal_id=config.principal, scopes={"operator"}
                )
                enabled = [
                    row for row in listed["subscriptions"] if row.get("enabled")
                ]
                if not enabled:
                    refresh_rows.append(
                        {
                            "domain": namespace,
                            "enabled_subscriptions": 0,
                            "feeds_checked": 0,
                            "feeds_failed": 0,
                        }
                    )
                    continue
                refreshed = inbox.refresh(
                    namespace,
                    principal_id=config.principal,
                    scopes={"operator"},
                    limit_per_feed=50,
                )
                refresh_rows.append(
                    {
                        "domain": namespace,
                        "enabled_subscriptions": len(enabled),
                        "feeds_checked": refreshed["feeds_checked"],
                        "feeds_failed": refreshed["feeds_failed"],
                        "results": refreshed["results"],
                    }
                )
                if refreshed["feeds_failed"]:
                    errors.append(
                        {
                            "stage": "refresh",
                            "domain": namespace,
                            "code": "feed_refresh_partial",
                            "count": refreshed["feeds_failed"],
                        }
                    )
            except Exception as exc:  # noqa: BLE001 - isolate one namespace
                error = _safe_error(exc)
                errors.append({"stage": "refresh", "domain": namespace, **error})
                refresh_rows.append({"domain": namespace, "error": error})
        receipt["stages"]["refresh"] = {
            "domains": refresh_rows,
            "network": "explicit-subscriptions-only",
        }

        remaining = int(max_items)
        ingested = 0
        skipped = 0
        workflow_receipts = []
        for namespace in domains:
            if remaining <= 0:
                break
            candidates = store.candidates(namespace, config.principal, remaining)
            remaining -= len(candidates)
            ready = []
            ready_items = []
            for item in candidates:
                if not str(item.get("content") or "").strip():
                    store.mark(
                        namespace,
                        config.principal,
                        item,
                        status="skipped",
                        document_id=None,
                        error_code="empty_content",
                    )
                    skipped += 1
                    continue
                ready.append(_feed_document(item, namespace))
                ready_items.append(item)
            if not ready:
                continue
            identity = "sync-feed:" + hashlib.sha256(
                _canonical(
                    [(item["item_id"], item["source_version"]) for item in ready_items]
                ).encode()
            ).hexdigest()[:24]
            try:
                result = ingest_documents(
                    config,
                    ready,
                    domain=namespace,
                    source_identity=identity,
                    conn=conn,
                )
                workflow_receipts.append(result)
                for item, document in zip(ready_items, ready, strict=True):
                    store.mark(
                        namespace,
                        config.principal,
                        item,
                        status="synced",
                        document_id=document.document_id,
                    )
                    ingested += 1
            except Exception as exc:  # noqa: BLE001 - isolate one domain ingest
                error = _safe_error(exc)
                errors.append({"stage": "ingest", "domain": namespace, **error})
                workflow_receipts.append({"domain": namespace, "error": error})
        receipt["stages"]["ingest"] = {
            "synced_items": ingested,
            "skipped_items": skipped,
            "backlog_items": store.backlog(config.principal),
            "max_items": int(max_items),
            "workflows": workflow_receipts,
        }

        try:
            membership = run_membership_pass(conn, registry)
            receipt["stages"]["membership"] = membership
        except Exception as exc:  # noqa: BLE001 - sync stage boundary
            error = _safe_error(exc)
            errors.append({"stage": "membership", **error})
            receipt["stages"]["membership"] = {"error": error}

        try:
            maintenance = MaintenanceOrchestrator(conn, root=config.root)
            recovered = maintenance.recover_stale(principal_id=config.principal)
            due = maintenance.due_work(limit=100)
            if _table_exists(conn, "source_pack_health"):
                health = maintenance.health()
            else:
                health = {
                    "contract": "noesis-maintenance-health-v1",
                    "status": "not-configured",
                    "at_ms": _now_ms(),
                    "reason": "no source-pack health records are configured",
                }
            receipt["stages"]["maintenance"] = {
                "mode": "inspect-and-recover-only",
                "recovered": recovered,
                "due": due,
                "health": health,
                "jobs_executed": 0,
            }
        except Exception as exc:  # noqa: BLE001 - sync stage boundary
            error = _safe_error(exc)
            errors.append({"stage": "maintenance", **error})
            receipt["stages"]["maintenance"] = {"error": error, "jobs_executed": 0}

        try:
            watch_watermark = _next_watch_watermark(conn)
            commit_watch_watermark(
                conn,
                watch_watermark,
                {
                    "sync_run_id": run["run_id"],
                    "synced_items": ingested,
                    "refresh_errors": sum(
                        1 for error in errors if error["stage"] == "refresh"
                    ),
                },
            )
            watches = run_watch_matcher(
                conn,
                registry,
                watch_watermark,
                principal_id=config.principal,
            )
            receipt["stages"]["watches"] = watches
            receipt["checkpoint"] = {"watch_watermark": watch_watermark}
        except Exception as exc:  # noqa: BLE001 - sync stage boundary
            error = _safe_error(exc)
            errors.append({"stage": "watches", **error})
            receipt["stages"]["watches"] = {"error": error}

        receipt["errors"] = errors
        receipt["status"] = "complete" if not errors else "partial"
    except Exception as exc:  # noqa: BLE001 - durable process boundary
        receipt["status"] = "failed"
        receipt["error"] = _safe_error(exc)
    finally:
        receipt["completed_at_ms"] = _now_ms()
        store.finish(run["run_id"], receipt["status"], receipt)
        conn.close()
    return receipt


def sync_once(config, *, max_items: int = 200, dry_run: bool = False) -> dict[str, Any]:
    with SyncLock(config.root):
        return _sync_pass(config, max_items=max_items, dry_run=dry_run)


def run_daemon(
    config,
    *,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    max_items: int = 200,
    dry_run: bool = False,
    stop_event: threading.Event | None = None,
    emit=None,
    max_cycles: int | None = None,
) -> dict[str, Any]:
    """Run persistent sync passes with bounded exponential backoff."""
    interval = float(interval_seconds)
    if not 1 <= interval <= 86_400:
        raise SyncError("invalid_interval", "interval must be between 1 and 86400 seconds")
    event = stop_event or threading.Event()
    cycles = 0
    failures = 0
    last = None
    with SyncLock(config.root):
        while not event.is_set():
            last = _sync_pass(config, max_items=max_items, dry_run=dry_run)
            cycles += 1
            if emit is not None:
                emit(last)
            failures = 0 if last["status"] in {"complete", "planned"} else min(failures + 1, 8)
            if max_cycles is not None and cycles >= max_cycles:
                break
            delay = min(interval * (2**failures), MAX_BACKOFF_SECONDS)
            event.wait(delay)
    return {
        "contract": "noesis-sync-daemon-v1",
        "status": "stopped",
        "cycles": cycles,
        "last": last,
    }


__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "MAX_ITEMS_PER_PASS",
    "SYNC_CONTRACT",
    "SyncError",
    "SyncLock",
    "run_daemon",
    "sync_once",
]
