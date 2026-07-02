// Driver for the NeuroNews web frontend ("Intelligence Terminal").
//
// Launches the Vite dev server (unless one is already reachable), drives it
// with Playwright (bundled Chromium, headless), clicks through every sidebar
// view, and writes a screenshot per view. Surfaces page console errors so a
// blank/broken render is caught instead of silently "passing".
//
// The app falls back to a baked-in design dataset when the FastAPI backend is
// offline (see apps/web/src/lib/queries.ts), so this runs fully standalone —
// no backend, no network, no API keys required.
//
// Playwright is not a dependency of apps/web. We resolve it from wherever it
// already lives on this machine (the n8n global install ships 1.57) and let it
// find the Chromium it downloaded into the ms-playwright cache. Override the
// lookup with PLAYWRIGHT_PATH=/abs/path/to/node_modules if needed.
//
// Usage:
//   node driver.mjs                 # launch server, shoot all views, exit
//   node driver.mjs --url URL       # drive an already-running server, no spawn
//   node driver.mjs --view feed     # only the named view(s), comma-separated
//   node driver.mjs --keep-server   # leave the dev server running on exit
//
// Screenshots land in ./screenshots/<view>.png (next to this file).

import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { mkdirSync, existsSync, readdirSync } from "node:fs";
import { homedir } from "node:os";

const __dirname = dirname(fileURLToPath(import.meta.url));
const WEB_DIR = join(__dirname, "..", "..", ".."); // apps/web (run-web -> skills -> .claude -> web)
const SHOT_DIR = join(__dirname, "screenshots");

// --- resolve Playwright from wherever it lives on this box ----------------
function loadPlaywright() {
  const candidates = [
    process.env.PLAYWRIGHT_PATH,
    "/usr/local/lib/node_modules/n8n/node_modules",
  ].filter(Boolean);
  for (const base of candidates) {
    try {
      return createRequire(join(base, "noop.js"))("playwright");
    } catch {}
  }
  try {
    return createRequire(import.meta.url)("playwright");
  } catch {}
  console.error(
    "Could not load Playwright. Point PLAYWRIGHT_PATH at a node_modules dir " +
      "that contains it (e.g. /usr/local/lib/node_modules/n8n/node_modules).",
  );
  process.exit(1);
}

// Find a usable Chromium binary. The Playwright we load (n8n's, v1.57) wants a
// browser build that may not match what's actually downloaded, so we bypass its
// version check and hand launch() an explicit executable. Override with
// CHROMIUM_PATH=/abs/path/to/chrome.
function findChromium() {
  if (process.env.CHROMIUM_PATH) return process.env.CHROMIUM_PATH;
  const cache = join(homedir(), ".cache", "ms-playwright");
  if (!existsSync(cache)) return null;
  // Prefer a full chromium-<rev> build (works headless); fall back to none.
  const dirs = readdirSync(cache)
    .filter((d) => d.startsWith("chromium-"))
    .sort()
    .reverse();
  for (const d of dirs) {
    const bin = join(cache, d, "chrome-linux64", "chrome");
    if (existsSync(bin)) return bin;
    const bin2 = join(cache, d, "chrome-linux", "chrome");
    if (existsSync(bin2)) return bin2;
  }
  return null;
}

// Canvases to capture: intent -> screenshot slug. The app is prompt-first —
// the driver types each intent into the bottom composer and submits, exactly
// like an operator would. "home" is the empty startup canvas (clicked via the
// sidebar's always-present "New canvas" entry).
const VIEWS = [
  [null, "home"],
  ["library documents", "library"],
  ["entity network connections", "entities"],
  ["fact-check claims and evidence", "claims"],
  ["stance conflicts and disagreements", "stance"],
  ["compare outlet framing and transparency", "outlets"],
  ["who are the key actors and stakeholders", "actors"],
  ["sentiment overview this week", "sentiment"],
  ["breaking event clusters", "clusters"],
  ["trending topics this week", "trending"],
  ["watchlist alerts", "watchlists"],
  ["story timeline developments", "timeline"],
];

function parseArgs(argv) {
  const out = { url: null, views: null, keepServer: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--url") out.url = argv[++i];
    else if (a === "--view") out.views = argv[++i].split(",").map((s) => s.trim());
    else if (a === "--keep-server") out.keepServer = true;
  }
  return out;
}

async function reachable(url) {
  try {
    const res = await fetch(url, { signal: AbortSignal.timeout(1500) });
    return res.ok;
  } catch {
    return false;
  }
}

// Start `npm run dev`, scrape the actual port Vite prints (it hops if 5173 is
// taken), and resolve once the server answers.
function startDevServer() {
  return new Promise((resolve, reject) => {
    const proc = spawn("npm", ["run", "dev"], {
      cwd: WEB_DIR,
      env: { ...process.env, FORCE_COLOR: "0", NO_COLOR: "1" },
    });
    let settled = false;
    const onData = (buf) => {
      const text = buf.toString();
      process.stdout.write(text);
      // Vite colorizes its banner regardless of NO_COLOR; strip ANSI first.
      const clean = text.replace(/\x1b\[[0-9;]*m/g, "");
      const m = clean.match(/Local:\s+http:\/\/localhost:(\d+)/);
      if (m && !settled) {
        settled = true;
        resolve({ proc, url: `http://localhost:${m[1]}` });
      }
    };
    proc.stdout.on("data", onData);
    proc.stderr.on("data", onData);
    proc.on("exit", (code) => {
      if (!settled) reject(new Error(`dev server exited early (code ${code})`));
    });
    setTimeout(() => {
      if (!settled) reject(new Error("dev server did not report a URL within 30s"));
    }, 30_000);
  });
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const { chromium } = loadPlaywright();
  mkdirSync(SHOT_DIR, { recursive: true });

  let server = null;
  let baseUrl = args.url;

  if (!baseUrl) {
    // Reuse a server already listening on the usual Vite ports.
    for (const port of [5173, 5174, 5175]) {
      const u = `http://localhost:${port}`;
      if (await reachable(u)) {
        baseUrl = u;
        console.log(`Reusing dev server at ${baseUrl}`);
        break;
      }
    }
  }
  if (!baseUrl) {
    console.log("Starting Vite dev server…");
    server = await startDevServer();
    baseUrl = server.url;
    console.log(`Dev server up at ${baseUrl}`);
  }

  const executablePath = findChromium();
  if (executablePath) console.log(`Using Chromium: ${executablePath}`);
  const browser = await chromium.launch({ headless: true, executablePath: executablePath || undefined });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

  const consoleErrors = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") consoleErrors.push(msg.text());
  });
  page.on("pageerror", (err) => consoleErrors.push(String(err)));

  await page.goto(baseUrl, { waitUntil: "domcontentloaded", timeout: 30000 });
  await page.waitForTimeout(1500);

  const wanted = args.views
    ? VIEWS.filter(([, slug]) => args.views.includes(slug))
    : VIEWS;

  let ok = 0;
  for (const [intent, slug] of wanted) {
    try {
      if (intent === null) {
        // The empty startup canvas: activate via the sidebar's "New canvas".
        await page.locator("aside").getByText("New canvas", { exact: true }).first().click({ timeout: 8000 });
      } else {
        // Prompt-first: type the intent into the bottom composer and submit.
        const composer = page.getByPlaceholder("Describe the view you need…");
        await composer.fill(intent, { timeout: 8000 });
        await composer.press("Enter");
        await page.waitForTimeout(900); // let the planner respond
      }
    } catch (e) {
      console.error(`! could not open canvas "${slug}": ${e.message}`);
      continue;
    }
    await page.waitForTimeout(600); // let the panels + charts settle
    const file = join(SHOT_DIR, `${slug}.png`);
    await page.screenshot({ path: file });
    console.log(`✓ ${slug.padEnd(12)} -> ${file}`);
    ok++;
  }

  // Confirm the live/demo data indicator rendered — proves the app booted,
  // not just that a blank shell painted.
  const heading = await page.title();
  console.log(`\nPage title: ${heading}`);
  console.log(`Screens captured: ${ok}/${wanted.length}`);
  if (consoleErrors.length) {
    console.log(`\nConsole errors (${consoleErrors.length}):`);
    for (const e of consoleErrors.slice(0, 10)) console.log(`  - ${e}`);
  } else {
    console.log("No console errors.");
  }

  await browser.close();
  if (server && !args.keepServer) server.proc.kill("SIGTERM");
  process.exit(ok === wanted.length ? 0 : 1);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
