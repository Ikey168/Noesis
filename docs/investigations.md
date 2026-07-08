# The investigation engine

Noesis is an investigation engine: it takes a question, holds competing
hypotheses against the ingested public record, and either reaches a cited
verdict or refuses to and names exactly what is missing. This document
describes the case model, the engine loop, the evidence discipline, and the
API and MCP surfaces.

The engine lives in `src/investigation/`. It is a pure composition layer: it
plans and pursues **leads** - replayable calls into the OSINT primitives
Noesis already builds (corroboration, contradiction scan, timelines, entity
dossiers, connection paths, source reliability) - and turns their output into
a durable, cited case file.

## The case model

A **case** is a first-class, durable object with five parts:

| Part | Table | What it holds |
|---|---|---|
| Record | `investigations` | question, topic/entity scope, status, verdict |
| Hypotheses | `investigation_hypotheses` | the competing readings the case weighs |
| Leads | `investigation_leads` | planned tool calls: open, pursued, or failed |
| Evidence | `investigation_evidence` | cited rows, weighted by source credibility |
| Journal | `investigation_events` | append-only: every step, oldest first |

Two properties are structural, not conventions:

- **Every case can state its own disconfirmation.** A case opened without
  explicit hypotheses gets the affirmative reading of its question *and* a
  null counterpart ("the record does not support: ..."). A case opened with
  a single hypothesis gets the null added. There is never a one-hypothesis
  case.
- **Every case replays.** Leads are keyed by a digest of `(tool, params)` and
  evidence rows by a digest of their identifying parts, so re-running a step
  converges instead of duplicating, and the journal records every open /
  plan / pursue / evaluate / conclude in order.

## The engine loop

```
open_case ──► plan_leads ──► pursue_open_leads ──► hypothesis_matrix ──► conclude_case
                  ▲                                        │                    │
                  └────────── new sources to vet ◄─────────┘         verdict, or gaps by name
```

1. **Plan.** Leads are derived from the case scope: every claim in the record
   matching the question's keywords gets a `corroborate` lead; the topic gets
   a `contradiction_scan` and a `timeline_reconstruct`; each scoped entity
   gets an `entity_dossier`, entity pairs a `relationship_path`. Planning is
   idempotent - a second round only surfaces genuinely new leads, including
   `source_reliability` vetting leads for every source the previous round's
   evidence introduced (the engine investigates its own witnesses).
2. **Claim alignment.** A matched claim is not necessarily *aligned* with the
   question - "flooding was minor" matches a question about severe flooding.
   The best-matching claim anchors the aligned side; any matched claim that
   carries a CONTRADICTS edge to an already-labelled claim takes the opposite
   label. Corroboration of an **opposed** claim counts with its direction
   flipped: a source contradicting the counter-claim supports the hypothesis.
3. **Pursue.** Each lead's output is harvested into evidence rows: one row
   per independent source, cited, weighted by the source's credibility
   (`outlet_scores` composite; 0.5 when unscored). Hypothesis-directed rows
   are mirrored into the null hypothesis with the relation flipped (a
   documented assumption of the method). Contradictions, timeline events,
   dossiers, connections, and vetting results are attached as **context**
   evidence - uncited entries flagged, never hidden. A single-sourced claim
   is journalled as a gap, not scored as corroboration.
4. **Evaluate.** `hypothesis_matrix` scores the case ACH-style: per
   hypothesis, the independent supporting and contradicting sources, the
   credibility-weighted tallies, and its **diagnostic sources** (sources
   supporting this hypothesis and no other - evidence that discriminates).
   The output carries the statistical-honesty envelope (`n`, `method`,
   `assumptions`) and a calibrated support-credibility interval for the
   leader; there is no single confidence number anywhere.
5. **Conclude.** The evidence-discipline gate. A verdict requires **all** of:
   - no planned lead left open,
   - the leader has ≥ 2 independent supporting sources,
   - the weighted margin over the runner-up is ≥ 0.5,
   - the leader carries less contradiction than support.

   When any check fails, `conclude_case` returns `concluded: false` with each
   gap named, journals the refusal, and leaves the case open. The engine
   never manufactures a verdict; when the null hypothesis wins the gate, the
   verdict states that the record does not support the question as posed.

`run_case` drives the whole loop with a round budget (default 3), stopping
early when a planning round yields nothing new.

## The case brief

`case_brief` renders a case as a finding: the question, the verdict (or what
is keeping the case open), the hypothesis ranking, the key evidence behind
the leader (mirror rows excluded), where the record disagrees with itself,
the noted gaps, and the engine's own footprint (leads pursued, sources
weighed, uncited rows flagged). `render_markdown` produces the human version;
uncited lines carry a visible `[UNCITED]` flag.

## HTTP surface

Registered in the API (no feature flag - case work is a core surface):

```
GET  /api/v1/investigation                     list cases
POST /api/v1/investigation/open               open a case
POST /api/v1/investigation/run                open + drive a whole case
POST /api/v1/investigation/{case_id}/advance  one plan/pursue round
POST /api/v1/investigation/{case_id}/conclude attempt a disciplined verdict
GET  /api/v1/investigation/{case_id}          the full case file
GET  /api/v1/investigation/{case_id}/matrix   the hypothesis matrix
GET  /api/v1/investigation/{case_id}/brief    the cited brief (?format=markdown)
```

Example:

```bash
curl -s -X POST localhost:8012/api/v1/investigation/run \
  -H 'content-type: application/json' \
  -d '{"question": "Severe flooding struck the delta region", "topic": "flooding"}'
```

## MCP surface

`tools/investigation_mcp/server.py` (registered as `noesis-investigation`)
exposes the same engine to agents and the generative UI:

| Tool | Kind | Does |
|---|---|---|
| `case_open` | write | open a case with competing hypotheses |
| `case_run` | write | open and drive a whole case |
| `case_advance` | write | one plan/pursue round |
| `case_conclude` | write | attempt a verdict through the gate |
| `case_list` | read | every case on the books |
| `case_file` | read | the full durable case file |
| `hypothesis_matrix` | read | ACH scoring (honesty-wrapped output schema) |
| `case_brief` | read | the cited brief, optionally rendered as markdown |

Write tools open the warehouse read-write under a process-level lock
(mirroring the provisioning server); read tools open it read-only.

## Relationship to the OSINT surface and agents

- The OSINT plane (`src/osint/`, `tools/osint_mcp/`) provides the
  *primitives*; the investigation engine provides the *drive*: state,
  direction, and the discipline gate. The engine calls the same functions
  the OSINT MCP tools call, in-process, on the injected connection.
- The provisioning-namespace "investigations" (`src/osint/investigations.py`,
  R11) remain what they were: provisioned KGs with audit trails. A case may
  be scoped to such a namespace's topic, but a case is about a *question*,
  not a storage namespace.
- The review gate is unaffected: the engine only ever plans leads over the
  ungated OSINT tools.

## Design notes

- **Stdlib-only, connection injected** - the same constraints as every other
  warehouse-side subsystem; writes run under the API's single-writer lock.
- **No stance model required.** Claim alignment uses the conflict edges the
  pipeline already computes. When a trained stance model lands, alignment can
  upgrade without changing the case model.
- **Honesty contract.** `hypothesis_matrix` validates against
  `src.analytics.honesty.validate_analytic_output`; the assumptions list
  names the mirroring and alignment heuristics explicitly.

Tests: `tests/unit/investigation/` (store, engine, report) and
`tests/unit/api/routes/test_investigation_routes.py`.
