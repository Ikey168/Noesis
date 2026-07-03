// Typed client for the agent host API (M10): launch the analyst / investigator
// agents on a goal and replay a run's audit trail. The run endpoints only work
// when the backend has NOESIS_AGENT_API enabled (GET /api/v1/agent reports it).

import type { UISpec } from "../genui/spec";

const BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");
const TIMEOUT_MS = 30000; // an agent run drives many tools; give it room

export class AgentApiError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
    this.name = "AgentApiError";
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
    if (!res.ok) throw new AgentApiError(`Request failed: ${path}`, res.status);
    return (await res.json()) as T;
  } catch (err) {
    if (err instanceof AgentApiError) throw err;
    throw new AgentApiError(err instanceof Error ? err.message : `Request error: ${path}`);
  } finally {
    clearTimeout(timer);
  }
}

export interface AgentKg {
  name: string;
  provisioned: boolean;
  status?: Record<string, unknown>;
}

export interface AgentFinding {
  tool: string;
  ok: boolean;
  result: Record<string, unknown>;
}

export interface AnalystResult {
  run_id: string;
  goal: string;
  kg: AgentKg;
  osint: AgentFinding[];
  canvas: UISpec;
  steps: number;
  findings: number;
}

export interface InvestigatorResult {
  run_id: string;
  title: string;
  kg: AgentKg;
  surface: AgentFinding[];
  audit: Record<string, unknown> | null;
  canvas: UISpec;
  steps: number;
  gated_calls: number;
  findings: number;
}

export interface RunCall {
  step: number;
  plane: string;
  server: string;
  tool: string;
  ok: boolean;
  arguments?: Record<string, unknown>;
}

export interface AnalystInput {
  goal: string;
  sources?: string[];
  topic?: string;
  entity?: string;
  claim_id?: string;
  source?: string;
}

export interface InvestigatorInput {
  title: string;
  entities?: string[];
  related_pair?: [string, string];
  topic?: string;
  claim_id?: string;
  sources?: string[];
}

export const agentApi = {
  status: () => call<{ enabled: boolean }>("/api/v1/agent"),

  analyst: (body: AnalystInput) =>
    call<AnalystResult>("/api/v1/agent/analyst", { method: "POST", body }),

  investigator: (body: InvestigatorInput) =>
    call<InvestigatorResult>("/api/v1/agent/investigator", { method: "POST", body }),

  run: (runId: string) =>
    call<{ run_id: string; calls: RunCall[]; count: number }>(
      `/api/v1/agent/runs/${encodeURIComponent(runId)}`,
    ),
};
