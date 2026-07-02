// Usage signals — the "adaptive" half of the generative UI. The canvas
// records which panel types the operator pins, dismisses and interacts
// with, persists them in localStorage, and feeds them back into layout
// generation (both the backend planner and the client fallback), so the
// canvas converges on the operator's habits.

import { useCallback, useState } from "react";
import type { PanelType } from "./spec";

export interface UsageSignals {
  pinned: PanelType[];
  dismissed: PanelType[];
  weights: Partial<Record<PanelType, number>>;
}

export const EMPTY_SIGNALS: UsageSignals = { pinned: [], dismissed: [], weights: {} };

const STORAGE_KEY = "noesis.genui.signals.v1";
export const MAX_WEIGHT = 20;

export function loadSignals(): UsageSignals {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return EMPTY_SIGNALS;
    const parsed = JSON.parse(raw) as Partial<UsageSignals>;
    return {
      pinned: Array.isArray(parsed.pinned) ? parsed.pinned : [],
      dismissed: Array.isArray(parsed.dismissed) ? parsed.dismissed : [],
      weights: parsed.weights && typeof parsed.weights === "object" ? parsed.weights : {},
    };
  } catch {
    return EMPTY_SIGNALS;
  }
}

function saveSignals(signals: UsageSignals): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(signals));
  } catch {
    // Storage unavailable (private mode) — adaptivity is session-only.
  }
}

export function hasSignals(s: UsageSignals): boolean {
  return s.pinned.length > 0 || s.dismissed.length > 0 || Object.keys(s.weights).length > 0;
}

export interface AdaptiveSignals {
  signals: UsageSignals;
  isPinned: (t: PanelType) => boolean;
  togglePin: (t: PanelType) => void;
  dismiss: (t: PanelType) => void;
  restore: (t: PanelType) => void;
  touch: (t: PanelType) => void;
  reset: () => void;
}

export function useAdaptiveSignals(): AdaptiveSignals {
  const [signals, setSignals] = useState<UsageSignals>(loadSignals);

  const update = useCallback((fn: (s: UsageSignals) => UsageSignals) => {
    setSignals((prev) => {
      const next = fn(prev);
      saveSignals(next);
      return next;
    });
  }, []);

  const togglePin = useCallback(
    (t: PanelType) =>
      update((s) => ({
        ...s,
        pinned: s.pinned.includes(t) ? s.pinned.filter((x) => x !== t) : [...s.pinned, t],
        dismissed: s.dismissed.filter((x) => x !== t),
      })),
    [update],
  );

  const dismiss = useCallback(
    (t: PanelType) =>
      update((s) => ({
        ...s,
        dismissed: s.dismissed.includes(t) ? s.dismissed : [...s.dismissed, t],
        pinned: s.pinned.filter((x) => x !== t),
      })),
    [update],
  );

  const restore = useCallback(
    (t: PanelType) => update((s) => ({ ...s, dismissed: s.dismissed.filter((x) => x !== t) })),
    [update],
  );

  const touch = useCallback(
    (t: PanelType) =>
      update((s) => ({
        ...s,
        weights: { ...s.weights, [t]: Math.min(MAX_WEIGHT, (s.weights[t] ?? 0) + 1) },
      })),
    [update],
  );

  const reset = useCallback(() => update(() => EMPTY_SIGNALS), [update]);

  return {
    signals,
    isPinned: (t) => signals.pinned.includes(t),
    togglePin,
    dismiss,
    restore,
    touch,
    reset,
  };
}
