// Typed client for the domain-pack ecosystem API (M9): discover published
// packs, install / uninstall them, and deploy their provisioning templates.
//
// Reads are always available; the mutating calls only succeed when the backend
// has NOESIS_PACKS_ADMIN enabled (the discover payload reports admin_enabled).

const BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");
const TIMEOUT_MS = 8000;

export class PacksApiError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
    this.name = "PacksApiError";
  }
}

async function call<T>(path: string, opts: { method?: string; body?: unknown } = {}): Promise<T> {
  const url = new URL(BASE_URL + path, BASE_URL || window.location.origin);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  const headers: Record<string, string> = { Accept: "application/json" };
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  try {
    const res = await fetch(url.toString(), {
      method: opts.method ?? "GET",
      headers,
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      signal: controller.signal,
    });
    if (!res.ok) throw new PacksApiError(`Request failed: ${path}`, res.status);
    return (await res.json()) as T;
  } catch (err) {
    if (err instanceof PacksApiError) throw err;
    throw new PacksApiError(err instanceof Error ? err.message : `Request error: ${path}`);
  } finally {
    clearTimeout(timer);
  }
}

export interface PackEntry {
  name: string;
  latest_version: string | null;
  versions: string[];
  description: string;
  source_types: string[];
  installed_version: string | null;
}

export interface PackDiscovery {
  admin_enabled: boolean;
  packs: PackEntry[];
  installed: Record<string, string>;
  count: number;
}

export interface PackInstallReport {
  name: string;
  version: string;
  panels: string[];
  enrichers: string[];
  ui_flags: Record<string, boolean>;
  planner_facets: string[];
  templates: string[];
}

export interface PackTemplate {
  name: string;
  description?: string;
  sources?: string[];
  backend?: string;
  pack?: string;
}

export const packsApi = {
  discover: () => call<PackDiscovery>("/api/v1/packs"),

  templates: () =>
    call<{ templates: PackTemplate[]; count: number }>("/api/v1/packs/templates"),

  install: (name: string, version?: string) =>
    call<{ installed: PackInstallReport }>("/api/v1/packs/install", {
      method: "POST",
      body: { name, version },
    }).then((r) => r.installed),

  uninstall: (name: string) =>
    call<{ uninstalled: string }>(`/api/v1/packs/${encodeURIComponent(name)}`, { method: "DELETE" }),

  deployTemplate: (name: string) =>
    call<{ deployed: Record<string, unknown> }>(
      `/api/v1/packs/templates/${encodeURIComponent(name)}/deploy`,
      { method: "POST" },
    ).then((r) => r.deployed),

  publish: (manifest: Record<string, unknown>, force = false) =>
    call<{ published: { name: string; version: string; path: string } }>("/api/v1/packs/publish", {
      method: "POST",
      body: { manifest, force },
    }).then((r) => r.published),
};
