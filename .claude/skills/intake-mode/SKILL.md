---
name: intake-mode
description: Run an information-intake-mode session in Noesis — the ten information workflow modes (Awareness, Exploration, Deep Research, Decision Support, Problem-Solving, Creation, Externalization, Internalization, Iteration, Maintenance). Use when the user wants to route a task to the right intake mode, start/pause/resume/complete a mode session, record evidence against a time budget, or export a session (incl. a Modulo handoff). Drives the noesis-knowledge-engine MCP (discover/route/start/command/export), contract noesis-intake-session-v1.
---

# Information intake mode sessions

Noesis models information work as **ten intake modes**, each a bounded session
with a time budget, an owner+namespace, a monotonic `revision`, a command
history, and a status. The session ledger (contract `noesis-intake-session-v1`)
is a coordination layer over existing Noesis stores — it holds **references**,
never copied source content. Tools live on the **`noesis-knowledge-engine`** MCP server
(`mcp__noesis-knowledge-engine__*`).

## The ten modes (default / min / max minutes)

| Mode | Budget | Routing intent (situation) |
|------|--------|----------------------------|
| Awareness | 15 / 1 / 15 | `staying_current` — keep up with a stream |
| Exploration | 90 / 60 / 120 | `curiosity_only` — open-ended look around |
| Deep Research | 120 | `systematic_understanding` — build real understanding |
| Decision Support | 60 | `decision_needed` — inform a specific decision |
| Problem-Solving | 45 | `urgent_or_broken` — something is broken/urgent |
| Creation | 120 | `creating` — produce an artifact |
| Externalization | 60 | `externalize` — get what's in your head out |
| Internalization | 30 | `internalize` — absorb/learn material |
| Iteration | 45 | `iterating` — refine existing work |
| Maintenance | 45 / 30 / 60 | `maintenance` — upkeep of an existing corpus |

## Workflow

1. **Discover** — `discover_intake_modes()` lists modes with budgets and per-mode
   readiness (`session_ledger_ready`, `native_workflow_ready`).
2. **Route** (optional) — `route_intake_mode(answers, override?)`. `answers` is a
   map of the ten intent questions above to booleans; the router returns the
   highest-priority match. A user can override with an explicit mode name.
3. **Start** — `start_intake_mode(namespace, mode, request_key, intent, …)`.
   - `request_key` must be **stable** for the logical start (safe replay: the
     same key returns the same session; a different payload under it fails).
   - Optional `duration_minutes` (clamped to the mode's min/max), `references`
     (`{kind,id,namespace,version,locator?}` to authoritative objects),
     `workspace_links` (opaque Modulo IDs), and `origin` (`{session_id, reason}`)
     to transition from a prior mode while keeping its lineage.
4. **Run** — `command_intake_mode(namespace, session_id, command_key,
   expected_revision, action, payload?)` with `action` ∈
   `record | pause | resume | complete | cancel`.
   - `expected_revision` must equal the session's current revision (optimistic
     concurrency); `command_key` must be stable per command (replay-safe).
   - `record` attaches evidence references/decisions; item decisions use
     `watch | escalate | schedule | discard | archive | flag`.
5. **Inspect / list** — `inspect_intake_mode(namespace, session_id)` and
   `list_intake_modes(namespace, …)` for current + historical revisions.
6. **Export** — `export_intake_mode(namespace, session_id)` yields every revision
   plus a SHA-256 digest; `verify_intake_mode_export(...)` checks integrity;
   `export_modulo_intake_handoff(...)` produces the Modulo return-navigation
   payload.

## Rules that matter

- **Scopes**: readers need `knowledge:intake:read` + `namespace:<name>:read`;
  writers need `knowledge:intake:write` + `namespace:<name>:write`. Over stdio the
  local principal/scopes apply.
- **No content copying** — attach a reference to the authoritative object; the
  ledger does not validate that a referenced object or Modulo link still exists,
  so resolve IDs through the owning subsystem before relying on them.
- **Revoked references** are redacted on inspection; `complete`/`export` fail
  while a linked access is missing.
- **Idempotency** — reuse `request_key`/`command_key` to retry safely; never
  reuse one for a different action/payload.

## Minimal example (Deep Research)

```
route_intake_mode(answers={"systematic_understanding": true})        # -> "Deep Research"
start_intake_mode(namespace="research", mode="Deep Research",
                  request_key="dr-levees-2026-09-22", intent="levee integrity in the delta")
# -> {session_id, revision: 0, ...}
command_intake_mode(namespace="research", session_id=..., command_key="dr-1",
                    expected_revision=0, action="record",
                    payload={"references": [{"kind":"claim","id":"...","namespace":"news","version":"..."}]})
command_intake_mode(..., command_key="dr-done", expected_revision=1, action="complete")
export_intake_mode(namespace="research", session_id=...)
```

For the Awareness mode's feed inbox → triage → promote workflow, use the
**awareness-inbox** skill.
