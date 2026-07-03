// Typed client for the persisted-canvas API (M8): save a canvas server-side,
// list and reopen saved canvases, and mint / open read-only share links.
//
// A canvas is owned by a stable, per-browser identity sent as X-Canvas-Owner;
// the backend scopes reads and writes to it. Shared links carry no owner and
// resolve read-only for anyone.

import type { UISpec } from "../genui/spec";

const BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");
const TIMEOUT_MS = 8000;
const OWNER_KEY = "noesis.canvas.owner";

export class CanvasApiError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
    this.name = "CanvasApiError";
  }
}

// A stable owner id for this browser, minted once and kept in localStorage.
function ownerId(): string {
  try {
    let id = window.localStorage.getItem(OWNER_KEY);
    if (!id) {
      const rand =
        typeof crypto !== "undefined" && "randomUUID" in crypto
          ? crypto.randomUUID()
          : Math.random().toString(36).slice(2);
      id = `web-${rand}`;
      window.localStorage.setItem(OWNER_KEY, id);
    }
    return id;
  } catch {
    return "web-local";
  }
}

async function call<T>(
  path: string,
  opts: { method?: string; body?: unknown; owner?: boolean } = {},
): Promise<T> {
  const url = new URL(BASE_URL + path, BASE_URL || window.location.origin);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  const headers: Record<string, string> = { Accept: "application/json" };
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (opts.owner !== false) headers["X-Canvas-Owner"] = ownerId();
  try {
    const res = await fetch(url.toString(), {
      method: opts.method ?? "GET",
      headers,
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      signal: controller.signal,
    });
    if (!res.ok) throw new CanvasApiError(`Request failed: ${path}`, res.status);
    return (await res.json()) as T;
  } catch (err) {
    if (err instanceof CanvasApiError) throw err;
    throw new CanvasApiError(err instanceof Error ? err.message : `Request error: ${path}`);
  } finally {
    clearTimeout(timer);
  }
}

// ---------- response shapes ----------

export interface SavedCanvas {
  id: string;
  owner: string;
  title: string;
  spec: UISpec;
  data_bindings: Record<string, unknown>;
  share_token: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface SavedCanvasSummary {
  id: string;
  title: string;
  topic: string | null;
  panel_count: number;
  shared: boolean;
  created_at: string | null;
  updated_at: string | null;
}

export interface SharedCanvas {
  id: string;
  title: string;
  spec: UISpec;
  data_bindings: Record<string, unknown>;
  read_only: true;
  created_at: string | null;
  updated_at: string | null;
}

export interface ShareLink {
  canvas_id: string;
  share_token: string;
  url: string;
}

// ---------- endpoint calls ----------

export const canvasApi = {
  save: (body: { spec: UISpec; title?: string; id?: string; data_bindings?: Record<string, unknown> }) =>
    call<{ canvas: SavedCanvas }>("/api/v1/ui/canvas", { method: "POST", body }).then((r) => r.canvas),

  list: () =>
    call<{ canvases: SavedCanvasSummary[]; count: number }>("/api/v1/ui/canvas").then((r) => r.canvases),

  get: (id: string) =>
    call<{ canvas: SavedCanvas }>(`/api/v1/ui/canvas/${encodeURIComponent(id)}`).then((r) => r.canvas),

  remove: (id: string) =>
    call<{ deleted: string }>(`/api/v1/ui/canvas/${encodeURIComponent(id)}`, { method: "DELETE" }),

  share: (id: string) =>
    call<ShareLink>(`/api/v1/ui/canvas/${encodeURIComponent(id)}/share`, { method: "POST" }),

  unshare: (id: string) =>
    call<{ canvas_id: string; revoked: boolean }>(
      `/api/v1/ui/canvas/${encodeURIComponent(id)}/share`,
      { method: "DELETE" },
    ),

  // The read-only viewer path: no owner header, resolves for anyone.
  getShared: (token: string) =>
    call<{ canvas: SharedCanvas }>(`/api/v1/ui/canvas/shared/${encodeURIComponent(token)}`, {
      owner: false,
    }).then((r) => r.canvas),
};
