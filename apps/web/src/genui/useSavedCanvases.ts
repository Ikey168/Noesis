// React-query hooks over the persisted-canvas API (M8): list saved canvases,
// fetch one by id, resolve a shared link, and the save / share / delete
// mutations that keep the list fresh.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { canvasApi, type SavedCanvas, type SavedCanvasSummary, type SharedCanvas } from "../lib/canvasStore";
import type { UISpec } from "./spec";

const LIST_KEY = ["savedCanvases"];

export function useSavedCanvases() {
  return useQuery<SavedCanvasSummary[]>({
    queryKey: LIST_KEY,
    queryFn: canvasApi.list,
    staleTime: 30_000,
    retry: false,
    // The API may be unreachable (dev without backend); an empty list keeps
    // the sidebar quiet rather than throwing.
    placeholderData: [],
  });
}

export function useSavedCanvas(id: string | null) {
  return useQuery<SavedCanvas>({
    enabled: !!id,
    queryKey: ["savedCanvas", id],
    queryFn: () => canvasApi.get(id as string),
    retry: false,
  });
}

export function useSharedCanvas(token: string | null) {
  return useQuery<SharedCanvas>({
    enabled: !!token,
    queryKey: ["sharedCanvas", token],
    queryFn: () => canvasApi.getShared(token as string),
    retry: false,
  });
}

export function useSaveCanvas() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { spec: UISpec; title?: string; id?: string }) => canvasApi.save(vars),
    onSuccess: () => qc.invalidateQueries({ queryKey: LIST_KEY }),
  });
}

export function useShareCanvas() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => canvasApi.share(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: LIST_KEY }),
  });
}

export function useDeleteCanvas() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => canvasApi.remove(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: LIST_KEY }),
  });
}
