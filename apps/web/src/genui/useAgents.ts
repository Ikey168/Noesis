// React-query hooks over the agent host API (M10): the enabled-status query and
// the analyst / investigator run mutations.

import { useMutation, useQuery } from "@tanstack/react-query";
import { agentApi, type AnalystInput, type InvestigatorInput } from "../lib/agentApi";

export function useAgentStatus() {
  return useQuery<{ enabled: boolean }>({
    queryKey: ["agent", "status"],
    queryFn: agentApi.status,
    staleTime: 60_000,
    retry: false,
  });
}

export function useRunAnalyst() {
  return useMutation({ mutationFn: (input: AnalystInput) => agentApi.analyst(input) });
}

export function useRunInvestigator() {
  return useMutation({ mutationFn: (input: InvestigatorInput) => agentApi.investigator(input) });
}
