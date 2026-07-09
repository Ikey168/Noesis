# ADR-001: Tool-to-panel annotation format

Status: accepted. Part of R2 (Discovery-derived catalog) in
`MCP_REARCHITECTURE_PLAN.md`; resolves that plan's open question 1.

## Context

Stage 1 derives the generative-UI panel catalog from the MCP servers
themselves: a tool that corresponds to a canvas panel announces that fact,
and the backend catalog builder (`src/genui/discovery.py`) turns the
annotation into a `PanelDef` at discovery time. Unannotated tools stay
invisible to the catalog by design.

Two candidate formats were considered:

1. **FastMCP tags** (`@mcp.tool(tags={"panel:claims"})`). Rejected: tags
   are a FastMCP-side filtering feature and their wire representation has
   moved between FastMCP releases (serialized under a private `_fastmcp`
   meta namespace when exposed at all). A catalog contract must not track
   a framework's private serialization.
2. **An explicit `panel` block in the tool's MCP `_meta` field**
   (`@mcp.tool(meta={"panel": {...}})`). Accepted: `_meta` is part of the
   MCP specification itself, round-trips through `tools/list` verbatim on
   any compliant client, and the block is plain data the builder can
   validate without knowing anything about the server framework.

## Decision

A panel-shaped tool declares:

```python
@mcp.tool(
    output_schema={"type": "object", "properties": {...}},
    meta={"panel": {
        "type": "claims",                          # required: renderer key
        "title": "Extracted claims",               # optional: panel heading
        "description": "Claims mined from ...",    # optional: for planners
        "endpoint": "/api/v1/arguments/claims",    # REST endpoint the panel fetches
        "facets": ["claims", "conflict"],          # required: intents it serves
        "tables": ["argument_claims"],             # availability hints
        "ui_flag": None,                           # domain-pack gate, if any
        "default_span": 6,                         # 12-column grid units
        "topic_param": "topic",                    # params mapping: the query
        "source_type_param": "source_type",        #   param names the endpoint
        "days_param": None,                        #   accepts (None = not taken)
        "max_days": None,                          # endpoint's days upper bound
    }},
)
def list_claims(...) -> dict: ...
```

Field semantics are exactly `PanelDef` in `src/genui/catalog.py` (the
static catalog remains the single source of truth for the *shape*; the
codegen from R0 keeps the frontend and contract in sync with it).

Rules enforced by the builder (`panel_def_from_annotation`):

- `type` is required, `^[a-z][a-z0-9_]{1,63}$`.
- `facets` is required and non-empty (list of non-empty strings).
- `default_span` is an int in 3..12 (default 6); `max_days` a positive
  int or absent; `tables` a list of strings.
- **`outputSchema` is required.** An annotated tool without a declared
  output schema is skipped with a warning: if a tool feeds a panel, its
  output must be introspectable. FastMCP derives a schema from the
  return annotation (`-> dict` and `-> list` both produce one), which
  satisfies the rule; an explicit `output_schema=` documenting the
  result fields is preferred. This is also the hook the Track DS
  statistical-honesty convention (R5) extends later.
- Anything malformed is skipped with a warning, never an error: a bad
  annotation must not break the catalog or the API.

Merge semantics (`merged_catalog`): the static catalog defines order and
remains the fallback; a discovered annotation for an existing type
overrides it in place; new types append after the static ones. With no
host, no connected servers, or no annotated tools, the merged catalog is
byte-identical to the static one (the R2 litmus). `GET /api/v1/ui/panels`
serves the merged catalog; each entry carries a `source` field (server
name or `"static"`) only when discovery contributed at least one def.

One tool maps to at most one panel block, and the first server (sorted
order) wins on duplicate types.

## Exemptions

Two catalog types are *composed* panels with no data tool behind them, by
design:

- `note` — the plan narrative, written by the planner itself.
- `timeline` — the story timeline, composed client-side (no backend
  endpoint exists).

They remain static-catalog-only; every other panel type has at least one
annotated tool counterpart (R2 exit criterion).

## Consequences

- Dropping in a new annotated server surfaces its panel type in
  `/api/v1/ui/panels` with zero genui code changes.
- The planner and `validate_spec` still read the static catalog; planning
  from discovered defs (and tool-sourced availability) is R3.
- Reference implementation: `list_claims` in
  `tools/argument_mcp/server.py`, round-trip verified against a live
  `tools/list` in the R2 verification run.
