---
name: run-web
description: Run, launch, start, build, screenshot, or smoke-test the NeuroNews Intelligence Terminal web frontend (apps/web — React + Vite + TypeScript SPA). Use when asked to see the web UI working, capture screenshots of a canvas (home, sentiment, claims, outlets, entity network, etc.), or verify a frontend change renders.
---

# Run the NeuroNews web frontend (Intelligence Terminal)

`apps/web` is a React 18 + Vite + TypeScript single-page app — a dark
"terminal" generative canvas: every screen is planned from an intent typed
into the ⌘K command bar in the top bar (the sidebar only manages open
canvases). Startup shows the ambient live-signal canvas; layouts appear on
ask. The driver types each intent into the command bar like an operator.
It fetches from the FastAPI backend (`services/api` / `src/api`) on every view
and **transparently falls back to a baked-in design dataset** when the backend
is unreachable (see `src/lib/queries.ts`), so it always renders standalone — no
backend, no API keys, no network required.

The agent path is the driver at `.claude/skills/run-web/driver.mjs`: it drives
the running app with headless Chromium (Playwright), clicks through every
sidebar entry, writes one screenshot per canvas, and reports page console errors.

**All paths below are relative to `apps/web/`.** `cd` there first.

## Prerequisites

- **Node 22** (`node --version` → v22.x) and npm. Already present on this box.
- **A Chromium binary.** The driver does not depend on Playwright being a
  project dependency — it resolves Playwright from wherever it already lives
  (the global n8n install ships v1.57 at
  `/usr/local/lib/node_modules/n8n/node_modules`) and points `launch()` at the
  Chromium already downloaded under `~/.cache/ms-playwright/`. No install step
  needed if those exist. Overrides if they live elsewhere:
  - `PLAYWRIGHT_PATH=/abs/node_modules` — dir containing the `playwright` package
  - `CHROMIUM_PATH=/abs/chrome` — explicit Chromium executable

Verify Chromium is present:

```bash
ls ~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome
```

## Setup

```bash
cd apps/web
npm install
```

## Run (agent path) — the driver

The driver needs a running dev server. Spawning the server from inside the
driver is unreliable under restrictive sandboxes (the child `npm` process can
be killed), so the **reliable two-step path** is: start the server yourself,
then drive it with `--url`.

**1. Start the dev server** (background; Vite hops to 5174/5175 if 5173 is
taken — read the port from the banner):

```bash
cd apps/web
npm run dev        # prints "Local: http://localhost:5173/"
```

**2. Drive it** (from `apps/web`, in another shell):

```bash
node .claude/skills/run-web/driver.mjs --url http://localhost:5173
```

Expected output — nine `✓` lines and a clean exit (code 0):

```
Using Chromium: /home/.../chromium-1228/chrome-linux64/chrome
✓ dashboard    -> .../screenshots/dashboard.png
✓ feed         -> .../screenshots/feed.png
... (graph, sentiment, clusters, trending, workspaces, watchlists, timeline)
Page title: NeuroNews · Intelligence Terminal
Screens captured: 9/9
```

Screenshots land in `.claude/skills/run-web/screenshots/<view>.png`
(1440×900). **Open one and look at it** — it should show the dark terminal UI
with data, not a blank page or error.

Shoot only specific views (comma-separated slugs: `dashboard`, `feed`, `graph`,
`sentiment`, `clusters`, `trending`, `workspaces`, `watchlists`, `timeline`):

```bash
node .claude/skills/run-web/driver.mjs --url http://localhost:5173 --view trending,sentiment
```

Driver flags: `--url URL` (drive an already-running server, no spawn),
`--view a,b` (subset of views), `--keep-server` (don't kill a driver-spawned
server on exit). With no `--url`, the driver first tries to reuse a server on
5173/5174/5175, then falls back to spawning one.

## Live vs. demo data

The app shows a `BACKEND LIVE` (green) / `DEMO MODE` pill in the top bar and a
`LIVE`/`DEMO` badge per panel, driven by a `/health` probe through the Vite
proxy to `http://localhost:8000`. Check whether a backend is up:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/health
```

- **`200`** → a backend is running; views show real data (`BACKEND LIVE`).
- **connection refused / non-200** → the app still renders fully from the
  design dataset (`DEMO MODE`). The driver and screenshots work either way.

To start the backend for live data (optional — see `apps/web/README.md`):

```bash
cd /home/Ikey/NeuroNews
NEURONEWS_DEV_MODE=true uvicorn src.api.app:app --reload --port 8000
```

`NEURONEWS_DEV_MODE=true` disables the WAF + rate limiting that otherwise block
the dashboard's burst of requests on load.

## Run (human path)

```bash
cd apps/web
npm run dev     # open http://localhost:5173 in a browser, Ctrl-C to stop
```

Useless headless — there's no browser to open the page in. Use the driver.

## Test / build

```bash
npm run typecheck   # tsc --noEmit
npm run build       # tsc --noEmit && vite build
```

## Gotchas

- **`@playwright/mcp` doesn't work out of the box here.** It defaults to the
  `chrome` channel and looks for Google Chrome at `/opt/google/chrome/chrome`,
  which isn't installed — only Playwright's bundled Chromium is. The driver
  sidesteps this by loading the `playwright` package directly and passing an
  explicit `executablePath`. Don't reach for the Playwright MCP tools; use the
  driver.
- **Playwright version vs. browser build mismatch.** The loaded Playwright
  (v1.57) expects browser build `1200`, but the cache has `chromium-1228`.
  Letting Playwright pick the browser fails with "Executable doesn't exist".
  The driver works around it by auto-discovering the newest `chromium-*` build
  in `~/.cache/ms-playwright/` and handing it to `launch({ executablePath })`.
- **Nav buttons aren't matchable by accessible name.** Each sidebar button
  wraps a glyph + label + badge (e.g. `≣ News Feed 3.8k`), so
  `getByRole('button', { name: 'News Feed' })` times out. The driver clicks the
  label text scoped to `<aside>` instead.
- **Vite colorizes its banner even with `NO_COLOR=1`/`FORCE_COLOR=0`.** The
  driver's spawn path strips ANSI escapes before scraping the port from the
  "Local:" line.
- **The `404` console error is benign** — a missing favicon/resource, not an
  app failure. The driver prints it but still exits 0 with all views captured.
- **DuckDB is single-writer.** If you run the live backend, stop it before
  running the news ingester (`python -m src.ingestion.scrapy_integration`);
  two writers conflict. Not needed for the demo-mode UI.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Could not load Playwright` | Set `PLAYWRIGHT_PATH` to a `node_modules` dir containing the `playwright` package (e.g. `/usr/local/lib/node_modules/n8n/node_modules`). |
| `browserType.launch: Executable doesn't exist at .../chromium_headless_shell-1200/...` | The cached Chromium build differs from what Playwright expects. The driver handles this automatically; if it still fails, set `CHROMIUM_PATH` to a real `chrome` binary (`ls ~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome`). |
| `dev server exited early` / driver hangs spawning | Use the two-step path: start `npm run dev` yourself, then run the driver with `--url http://localhost:<port>`. |
| `could not click nav "<label>"` | The dev server isn't fully loaded, or a sidebar label changed in `src/components/Sidebar.tsx`. Confirm the page loads in a browser; update the `VIEWS` list in `driver.mjs` if labels were renamed. |
| All panels show `DEMO` | No backend on :8000 — expected and fine for driving the UI. Start the backend (see Live vs. demo data) only if you need real data. |
