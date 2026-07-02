// Canvas manager — the generative replacement for fixed views. A canvas is
// a named intent; the whole app is a list of canvases, persisted in
// localStorage, with "Briefing" (the empty intent) always present. Sidebar
// presets and free-typed intents both open canvases here.

import { useCallback, useState } from "react";

export interface CanvasDef {
  id: string;
  label: string;
  intent: string;
}

// The home canvas is intentionally empty: startup shows a bare surface
// with just the prompt composer — nothing is generated until asked.
export const HOME: CanvasDef = { id: "home", label: "New canvas", intent: "" };

const STORAGE_KEY = "noesis.genui.canvases.v2";

interface Stored {
  canvases: CanvasDef[];
  activeId: string;
}

function normalizeIntent(intent: string): string {
  return intent.trim().slice(0, 500);
}

export function labelForIntent(intent: string): string {
  const trimmed = intent.trim().replace(/\s+/g, " ");
  if (!trimmed) return HOME.label;
  const capped = trimmed.charAt(0).toUpperCase() + trimmed.slice(1);
  return capped.length > 30 ? capped.slice(0, 29) + "…" : capped;
}

function load(): Stored {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<Stored>;
      const rest = (Array.isArray(parsed.canvases) ? parsed.canvases : []).filter(
        (c): c is CanvasDef =>
          !!c && typeof c.id === "string" && typeof c.label === "string" && typeof c.intent === "string" && c.id !== HOME.id,
      );
      const canvases = [HOME, ...rest];
      const activeId = canvases.some((c) => c.id === parsed.activeId) ? (parsed.activeId as string) : HOME.id;
      return { canvases, activeId };
    }
  } catch {
    // Corrupt storage — start fresh.
  }
  return { canvases: [HOME], activeId: HOME.id };
}

function save(state: Stored): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch {
    // Storage unavailable — canvases are session-only.
  }
}

export interface CanvasManager {
  canvases: CanvasDef[];
  active: CanvasDef;
  setActive: (id: string) => void;
  open: (intent: string, label?: string) => void;
  remove: (id: string) => void;
}

export function useCanvases(): CanvasManager {
  const [state, setState] = useState<Stored>(load);

  const update = useCallback((fn: (s: Stored) => Stored) => {
    setState((prev) => {
      const next = fn(prev);
      save(next);
      return next;
    });
  }, []);

  const setActive = useCallback(
    (id: string) =>
      update((s) => (s.canvases.some((c) => c.id === id) ? { ...s, activeId: id } : s)),
    [update],
  );

  const open = useCallback(
    (intent: string, label?: string) =>
      update((s) => {
        const normalized = normalizeIntent(intent);
        if (!normalized) return { ...s, activeId: HOME.id };
        const existing = s.canvases.find((c) => c.intent === normalized);
        if (existing) return { ...s, activeId: existing.id };
        const canvas: CanvasDef = {
          id: `c-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`,
          label: label ?? labelForIntent(normalized),
          intent: normalized,
        };
        return { canvases: [...s.canvases, canvas], activeId: canvas.id };
      }),
    [update],
  );

  const remove = useCallback(
    (id: string) =>
      update((s) => {
        if (id === HOME.id) return s;
        const canvases = s.canvases.filter((c) => c.id !== id);
        return { canvases, activeId: s.activeId === id ? HOME.id : s.activeId };
      }),
    [update],
  );

  const active = state.canvases.find((c) => c.id === state.activeId) ?? HOME;
  return { canvases: state.canvases, active, setActive, open, remove };
}
