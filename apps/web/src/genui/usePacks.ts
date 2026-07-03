// React-query hooks over the domain-pack ecosystem API (M9): discover packs and
// the install / uninstall / deploy-template mutations that keep the list fresh.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { packsApi, type PackDiscovery, type PackTemplate } from "../lib/packsApi";

const DISCOVER_KEY = ["packs", "discover"];

export function usePackDiscovery() {
  return useQuery<PackDiscovery>({
    queryKey: DISCOVER_KEY,
    queryFn: packsApi.discover,
    staleTime: 30_000,
    retry: false,
  });
}

export function usePackTemplates() {
  return useQuery<PackTemplate[]>({
    queryKey: ["packs", "templates"],
    queryFn: () => packsApi.templates().then((r) => r.templates),
    staleTime: 30_000,
    retry: false,
    placeholderData: [],
  });
}

export function useInstallPack() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { name: string; version?: string }) =>
      packsApi.install(vars.name, vars.version),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["packs"] }),
  });
}

export function useUninstallPack() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => packsApi.uninstall(name),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["packs"] }),
  });
}

export function useDeployTemplate() {
  return useMutation({
    mutationFn: (name: string) => packsApi.deployTemplate(name),
  });
}

export function usePublishPack() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { manifest: Record<string, unknown>; force?: boolean }) =>
      packsApi.publish(vars.manifest, vars.force),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["packs"] }),
  });
}
