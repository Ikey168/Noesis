"""
Agent audit trail and replay (M10.4).

Every action an agent takes through the runtime (M10.1) is written to the
**provisioning audit trail** - the same append-only ``provisioning_events`` log
that records deploy/attach/ingest/teardown - so an agent run lives alongside the
provisioning lineage it drives and is reconstructable the same way.

:func:`provisioning_audit_sink` returns an :class:`AgentRuntime` audit sink that
appends one event per tool call, grouped under a ``run_id``. :func:`replay_run`
reads those events back in order and reconstructs the exact call sequence, so a
run can be replayed from the trail alone - the M10.4 accountability guarantee.

Stdlib-only; the connection is injected.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from src.agent.runtime import ToolCall

# The event name under which an agent tool call is logged.
AGENT_EVENT = "agent_call"


def provisioning_audit_sink(
    conn,
    run_id: str,
    lock: Any = None,
    clock: Optional[Callable[[], Any]] = None,
) -> Callable[[ToolCall], None]:
    """An audit sink that writes each agent call to the provisioning audit trail,
    grouped under ``run_id``. Attach it to an :class:`AgentRuntime` as
    ``audit_sink`` and every call the agent makes is recorded."""
    from datetime import datetime, timezone

    from src.provisioning import store

    def _now():
        if clock is not None:
            return clock()
        return datetime.now(timezone.utc)

    if lock is not None:
        with lock:
            store.ensure_schema(conn)
    else:
        store.ensure_schema(conn)

    def sink(call: ToolCall) -> None:
        detail = dict(call.summary())
        detail["run_id"] = run_id
        now = _now()
        if lock is not None:
            with lock:
                store.record_event(conn, run_id, AGENT_EVENT, detail, now)
        else:
            store.record_event(conn, run_id, AGENT_EVENT, detail, now)

    return sink


def replay_run(conn, run_id: str, limit: int = 1000) -> List[Dict[str, Any]]:
    """Reconstruct an agent run's ordered call sequence from the audit trail.
    Returns the per-call records (plane, server, tool, arguments, ok, step) in
    execution order."""
    from src.provisioning import store

    events = store.list_events(conn, name=run_id, limit=limit)
    calls = [e["detail"] for e in events if e.get("event") == AGENT_EVENT]
    # list_events is newest-first; order by the recorded step for exact replay.
    calls.sort(key=lambda d: d.get("step", 0))
    return calls


__all__ = ["AGENT_EVENT", "provisioning_audit_sink", "replay_run"]
