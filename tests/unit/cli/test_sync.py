from __future__ import annotations

import json

import pytest

from src import sync_runtime
from src.kb.contract import kb_documents
from src.kb.intake_inbox import IntakeInboxStore
from src.noesis_cli.app import build_parser, main
from src.noesis_cli.config import initialize, open_warehouse
from src.sync_runtime import SyncError, SyncLock, run_daemon, sync_once


def _config(tmp_path):
    config, _ = initialize(config_path=tmp_path / ".noesis" / "config.json")
    return config


def _seed_feed_item(config):
    conn = open_warehouse(config)
    try:
        inbox = IntakeInboxStore(conn)
        subscription = inbox.subscribe(
            "local",
            "https://example.com/feed.xml",
            "Example",
            "rss_atom",
            principal_id=config.principal,
            scopes={"operator"},
        )
        inbox.ingest(
            "local",
            subscription["subscription_id"],
            [
                {
                    "url": "https://example.com/post-1",
                    "title": "Sync smoke",
                    "content": "The persistent sync loop indexed this durable result.",
                    "published_at_ms": 1_700_000_000_000,
                }
            ],
            principal_id=config.principal,
            scopes={"operator"},
        )
        # Keep the canonicalization test offline: the item is already acquired.
        conn.execute(
            "UPDATE intake_inbox_subscriptions SET enabled=false "
            "WHERE subscription_id=?",
            [subscription["subscription_id"]],
        )
    finally:
        conn.close()


def test_sync_dry_run_does_not_create_sync_tables(tmp_path):
    config = _config(tmp_path)
    conn = open_warehouse(config)
    try:
        before = conn.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_name LIKE 'noesis_sync_%'"
        ).fetchone()[0]
    finally:
        conn.close()

    result = sync_once(config, dry_run=True)

    assert result["status"] == "planned"
    assert result["plan"]["research_execution"] is False
    assert result["plan"]["paid_acquisition"] is False
    conn = open_warehouse(config)
    try:
        after = conn.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_name LIKE 'noesis_sync_%'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert before == after == 0


def test_sync_indexes_feed_revision_once_and_persists_checkpoint(tmp_path):
    config = _config(tmp_path)
    _seed_feed_item(config)

    first = sync_once(config, max_items=20)
    second = sync_once(config, max_items=20)

    assert first["status"] == "complete"
    assert first["stages"]["ingest"]["synced_items"] == 1
    assert first["stages"]["ingest"]["backlog_items"] == 0
    assert second["status"] == "complete"
    assert second["stages"]["ingest"]["synced_items"] == 0

    conn = open_warehouse(config)
    try:
        documents = kb_documents(
            "local", limit=100, conn=conn, config_path=config.domains
        )["data"]
        feed_state = conn.execute(
            "SELECT source_version,status,document_id FROM noesis_sync_feed_state"
        ).fetchone()
        last_success = conn.execute(
            "SELECT value_json FROM noesis_sync_state WHERE key='last_success'"
        ).fetchone()
        run_statuses = conn.execute(
            "SELECT status FROM noesis_sync_runs ORDER BY sequence"
        ).fetchall()
    finally:
        conn.close()

    assert any(row["document_id"].startswith("feed:") for row in documents)
    assert feed_state[0] == 1
    assert feed_state[1] == "synced"
    assert feed_state[2].startswith("feed:")
    assert json.loads(last_success[0])["run_id"] == second["run_id"]
    assert run_statuses == [("complete",), ("complete",)]


def test_sync_recovers_interrupted_run_receipt(tmp_path):
    config = _config(tmp_path)
    conn = open_warehouse(config)
    try:
        stale = sync_runtime.SyncStore(conn).start()
    finally:
        conn.close()

    result = sync_once(config, max_items=10)

    assert result["status"] == "complete"
    assert result["recovered_interrupted_runs"] == 1
    conn = open_warehouse(config)
    try:
        rows = conn.execute(
            "SELECT run_id,status FROM noesis_sync_runs ORDER BY sequence"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [(stale["run_id"], "interrupted"), (result["run_id"], "complete")]


def test_sync_lock_is_single_instance(tmp_path):
    config = _config(tmp_path)
    with (
        SyncLock(config.root),
        pytest.raises(SyncError) as exc,
        SyncLock(config.root),
    ):
        pass
    assert exc.value.code == "sync_already_running"


def test_daemon_uses_bounded_backoff_and_resets_on_success(
    tmp_path, monkeypatch
):
    config = _config(tmp_path)
    statuses = iter(("partial", "partial", "complete"))
    emitted = []
    waits = []

    def fake_pass(_config, *, max_items, dry_run):
        return {"contract": "noesis-sync-v1", "status": next(statuses)}

    class FakeEvent:
        def is_set(self):
            return False

        def wait(self, delay):
            waits.append(delay)
            return False

    monkeypatch.setattr(sync_runtime, "_sync_pass", fake_pass)
    result = run_daemon(
        config,
        interval_seconds=2,
        max_items=10,
        stop_event=FakeEvent(),
        emit=emitted.append,
        max_cycles=3,
    )

    assert result["cycles"] == 3
    assert [row["status"] for row in emitted] == [
        "partial",
        "partial",
        "complete",
    ]
    assert waits == [4.0, 8.0]


def test_cli_sync_dry_run_and_daemon_parser(tmp_path, capsys):
    config = _config(tmp_path)
    code = main(
        [
            "--config",
            str(config.path),
            "sync",
            "--dry-run",
            "--json",
        ]
    )
    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert output["command"] == "sync"
    assert output["data"]["status"] == "planned"

    args = build_parser().parse_args(
        ["sync", "--daemon", "--interval", "42", "--max-items", "17"]
    )
    assert args.daemon is True
    assert args.interval == 42.0
    assert args.max_items == 17
