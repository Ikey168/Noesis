# M10 acceptance: agent runs, accountable by replay

Milestone M10 (issues #691-#694) puts an agent on top of Noesis: a runtime over
the MCP surface, an analyst agent, an investigator agent, and full audit-trail
coverage. This is the acceptance record for the accountability guarantee; its
executable form is `scripts/agent/m10_acceptance.py`, run in CI by
`tests/unit/agent/test_agent_audit.py`.

## The agent stack

- **M10.1** `src/agent/runtime.py` - the runtime: a budgeted, allowlisted, audited
  gate between an agent and the provisioning / OSINT / genui planes. It refuses
  un-allowlisted tools, refuses gated OSINT tools while the review gate is closed,
  and records every call.
- **M10.2** `src/agent/analyst.py` - the analyst: goal -> provisioned KG -> OSINT
  -> canvas.
- **M10.3** `src/agent/investigator.py` - the investigator: opens an investigation
  KG, drives the R11 surface (dossier, path, timeline, trace), never touches a
  gated tool while the gate is off.
- **M10.4** `src/agent/audit.py` - the audit trail: every agent call is written to
  the same `provisioning_events` log that records KG lineage, and `replay_run`
  reconstructs a run from it.

## What the acceptance proves

The harness runs the analyst end to end with the provisioning audit sink attached
to the runtime, then reconstructs the run from the audit trail alone:

1. **Full coverage.** Every tool call the agent made is written to the
   provisioning audit trail (one `agent_call` event per call, grouped under a
   `run_id`).
2. **Reconstructable.** Replaying the trail yields the same ordered sequence of
   `(plane, server, tool, arguments, ok)` as the live run - call for call.

Failed calls are recorded too (with `ok=false`), so the record never hides what
happened.

## Result

```
1. analyst run: steps=9, findings=3, kg='flooding_in_the_coastal_delta' provisioned=True
2. audit trail: 9 events recorded for 9 calls; complete=True
3. replay: sequence_matches=True, arguments_match=True

RESULT: OK - the run is fully reconstructable from the audit trail
```

## Why this matters

An agent that provisions KGs, runs OSINT, and builds canvases is only trustworthy
if every action it took is on the record. By writing agent calls to the same
audit trail that already makes provisioning replayable, an agent run inherits the
same accountability: nothing the agent did is invisible, and the whole run can be
replayed from the log.
