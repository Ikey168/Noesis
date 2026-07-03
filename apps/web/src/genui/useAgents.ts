// React-query hooks over the agent host API (M10): the enabled-status query and
// the analyst / investigator run mutations.

import { useMutation, useQuery } from "@tanstack/react-query";
import { agentApi, type AnalystInput, type InvestigatorInput, type RunCall } from "../lib/agentApi";

export function useAgentStatus() {
  return useQuery<{ enabled: boolean }>({
    queryKey: ["agent", "status"],
    queryFn: agentApi.status,
    staleTime: 60_000,
    retry: false,
  });
}

export function useRunReplay(runId: string | null, enabled: boolean) {
  return useQuery<{ run_id: string; calls: RunCall[]; count: number }>({
    enabled: enabled && !!runId,
    queryKey: ["agent", "run", runId],
    queryFn: () => agentApi.run(runId as string),
    retry: false,
  });
}

export function useRunAnalyst() {
  return useMutation({ mutationFn: (input: AnalystInput) => agentApi.analyst(input) });
}

export function useRunInvestigator() {
  return useMutation({ mutationFn: (input: InvestigatorInput) => agentApi.investigator(input) });
}
