---
name: add-genui-panel
description: Add a new panel type to the Noesis generative canvas (apps/web + src/genui). Use when the canvas should be able to render a new kind of content — a new chart, list, or metric — that the planner can select for matching intents. Covers the backend catalog, the codegen that derives the contract enums and frontend catalog from it, the frontend registry, and the tests that keep them in sync.
---

# Add a panel type to the Noesis generative canvas

The UI has no fixed views: every screen is a `ui-spec-v1` layout planned from
an intent and rendered from a panel registry. `src/genui/catalog.py` is the
single source of truth for panel types: the contract enums and the frontend
catalog (`catalog.gen.ts`) are **generated** from it. Adding a capability
means one catalog entry + one codegen run + one renderer. Skip the codegen
and the tests (and CI) fail on staleness; skip the renderer and the frontend
shows a "not installed" stub.

**Paths are relative to the repo root.**

## The 5 touch-points

1. **`src/genui/catalog.py`** — add a `PanelDef` to `PANEL_CATALOG`:

   ```python
   PanelDef(
       type="my_panel",                    # renderer key, snake_case
       title="My panel",
       description="One line for the LLM planner and /api/v1/ui/panels.",
       endpoint="/api/v1/my_thing",        # or None for client-side data
       facets=("overview",),               # which intents select it
       tables=("my_table",),               # warehouse tables it needs (availability gate)
       ui_flag=None,                       # domain-pack flag gate, if any
       default_span=6,                     # 12-column grid units
       topic_param="topic",                # param names, if the endpoint takes them
       source_type_param="source_type",
       days_param="days", max_days=30,     # match the endpoint's Query(le=...)
   )
   ```

   Catalog order matters: within a facet, earlier panels get higher priority.

2. **Run the codegen** — regenerates the contract enums
   (`contracts/schemas/jsonschema/ui-spec-v1.json`) and the frontend catalog
   (`apps/web/src/genui/catalog.gen.ts`, used by the offline client planner
   and pinned-panel rendering) from `catalog.py`:

   ```bash
   python scripts/genui/codegen.py
   ```

   Never edit those files by hand — `tests/unit/genui/test_codegen.py` and
   the CI staleness gate fail until they match a fresh render.

3. **`apps/web/src/genui/registry.tsx`** — write the renderer and register it:

   ```tsx
   function MyPanel(props: PanelProps) {
     const { data, source, isLoading } = useMyThing();   // hook from lib/queries.ts
     return (
       <GenPanel {...props} source={source} isLoading={isLoading}>
         {data.length === 0 ? <Empty text="No data yet" /> : /* rows */}
       </GenPanel>
     );
   }
   // ...and in REGISTRY: my_panel: MyPanel,
   ```

   Read panel params defensively (`props.panel.params?.topic` may be any
   scalar — see `topicMatch`/`daysParam` helpers). Hooks must be called
   unconditionally (hooks rules); reuse or extend `lib/queries.ts` hooks,
   which give you the live/demo fallback and `SourceBadge` provenance.

4. **Planner keywords (only if the panel needs a new facet)** — add the facet
   to `FACETS` in `catalog.py` (the codegen carries it into the contract enum
   and the frontend `Facet` union), then add keywords to `FACET_KEYWORDS` in
   BOTH `src/genui/planner.py` and `apps/web/src/genui/planner.ts` (keep them
   word-for-word identical — a drift means live and offline plans diverge;
   keywords are not yet generated).

5. **Tests** — extend `tests/unit/genui/` (planner selects the panel for a
   matching intent; catalog dict shape) and, if you added params, assert they
   land on the panel. Run:

   ```bash
   python3 -m pytest tests/unit/genui tests/unit/api/routes/test_genui_routes_smoke.py -q
   cd apps/web && npm run typecheck
   ```

## Verify

```bash
python3 - <<'PY'
from src.genui import plan, validate_spec
spec = plan("an intent that should select your panel")
print([p.type for p in spec.panels])
assert validate_spec(spec.to_dict()) == []
PY
```

Then screenshot with the run-web driver (add a preset to
`src/components/Sidebar.tsx` if the panel deserves a sidebar entry, and its
label to the driver's `VIEWS`):

```bash
node .claude/skills/run-web/driver.mjs --url http://localhost:5173 --view briefing
```

## Gotchas

- **One catalog, generated mirrors.** `catalog.py` is the only place a type
  string is authored; the contract enum and `catalog.gen.ts` come from the
  codegen, and `tests/unit/genui/test_codegen.py` fails while they are stale.
  A missing frontend registry entry renders a "not installed" stub rather
  than crashing — visible in screenshots.
- **`tables` drives adaptivity.** If the warehouse table is empty the panel
  is silently dropped from generated layouts (by design). Leave `tables=()`
  for panels fed by non-warehouse or client-side data.
- **`max_days` must match the endpoint's `Query(le=...)`** or generated
  layouts will 422 against their own endpoint and the panel falls to demo.
- **Params reach the renderer as untrusted scalars** (the LLM planner can
  emit anything that validates). Type-check before use.
