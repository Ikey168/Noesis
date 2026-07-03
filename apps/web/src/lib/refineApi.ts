// Client for in-canvas refinement (M6): POST /api/v1/ui/refine turns an
// instruction into a spec diff and applies it to the current canvas in place.

import type { UISpec } from "../genui/spec";

const BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");
const TIMEOUT_MS = 8000;

export class RefineApiError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
    this.name = "RefineApiError";
  }
}

export interface RefineResponse {
  spec: UISpec;
  diff: Array<Record<string, unknown>>;
  errors: string[];
  changed: boolean;
}

export async function refineUi(body: { spec: UISpec; instruction: string }): Promise<RefineResponse> {
  const url = new URL(BASE_URL + "/api/v1/ui/refine", BASE_URL || window.location.origin);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(url.toString(), {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    if (!res.ok) throw new RefineApiError("refine failed", res.status);
    return (await res.json()) as RefineResponse;
  } catch (err) {
    if (err instanceof RefineApiError) throw err;
    throw new RefineApiError(err instanceof Error ? err.message : "refine error");
  } finally {
    clearTimeout(timer);
  }
}
