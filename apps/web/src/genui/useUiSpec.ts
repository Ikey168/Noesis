// Layout-generation hook. Asks the backend planner for a ui-spec-v1 layout
// and transparently falls back to the client-side planner when the API is
// unreachable — same live/demo contract as the data hooks in lib/queries.ts.

import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import type { Source } from "../lib/queries";
import type { UISpec } from "./spec";
import { localPlan } from "./planner";
import type { UsageSignals } from "./signals";

export interface UiSpecResult {
  spec: UISpec;
  source: Source;
  isLoading: boolean;
}

// Interaction weights are a soft ranking nudge applied on the NEXT
// regeneration (new intent / pin / mute) — keying the query on them would
// re-plan the whole canvas on every panel click.
function signalsKey(s: UsageSignals): string {
  return JSON.stringify([s.pinned, s.dismissed]);
}

export function useUiSpec(intent: string, signals: UsageSignals, enabled = true): UiSpecResult {
  // The backend contract caps intent at 500 chars; over-long input must
  // degrade by truncation, not by a 422 that silently downgrades to the
  // client planner.
  const bounded = intent.slice(0, 500);
  const q = useQuery({
    // Empty canvases generate nothing: the query only runs once an intent
    // is submitted (the startup surface stays blank by design).
    enabled,
    queryKey: ["uiSpec", bounded, signalsKey(signals)],
    queryFn: async (): Promise<{ spec: UISpec; source: Source }> => {
      try {
        const res = await api.generateUi({
          intent: bounded,
          signals: {
            pinned: signals.pinned,
            dismissed: signals.dismissed,
            weights: signals.weights as Record<string, number>,
          },
        });
        if (!res.spec || !Array.isArray(res.spec.panels) || res.spec.panels.length === 0) {
          throw new Error("empty spec");
        }
        return { spec: res.spec, source: "live" };
      } catch {
        return { spec: localPlan(bounded, signals), source: "demo" };
      }
    },
    staleTime: 60_000,
    retry: false,
    placeholderData: (prev) => prev,
  });

  return {
    spec: q.data?.spec ?? localPlan(bounded, signals),
    source: q.data?.source ?? "demo",
    isLoading: q.isLoading,
  };
}
