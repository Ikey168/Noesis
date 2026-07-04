// The generative canvas — the app's only surface. An empty canvas is not a
// chat waiting for input: it shows the live signal already flowing through
// the pipeline (entity constellation, movers, ingest stats) and points at
// the ⌘K command bar. With an intent, the planned layout fills the surface.

import { useEffect, useState } from "react";
import { RotateCcw } from "lucide-react";
import SpecRenderer from "./SpecRenderer";
import SavedCanvasView from "./SavedCanvasView";
import CanvasActions from "./CanvasActions";
import RefineBar from "./RefineBar";
import type { UISpec } from "./spec";
import EntityGraph from "../components/charts/EntityGraph";
import { useArticles, useClusters, useEntityGraph, useUiTelemetry } from "../lib/queries";
import { useUiSpec } from "./useUiSpec";
import { useAdaptiveSignals, hasSignals } from "./signals";
import { PANEL_DEFS } from "./spec";
import type { CanvasDef } from "./canvases";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";

const PLANNER_BADGE: Record<string, { label: string; className: string; hint: string }> = {
  llm: {
    label: "LLM PLAN",
    className: "border-violet-400/30 bg-violet-400/10 text-violet-400",
    hint: "Layout composed by the configured LLM planner",
  },
  heuristic: {
    label: "RULE PLAN",
    className: "border-teal-400/30 bg-teal-400/10 text-teal-400",
    hint: "Layout composed by the backend heuristic planner",
  },
  client: {
    label: "LOCAL PLAN",
    className: "border-amber-400/30 bg-amber-400/10 text-amber-400",
    hint: "Backend unreachable — layout composed in the browser",
  },
};

const QUIET_SUGGESTIONS = ["daily briefing", "fact-check claims about the economy", "library documents"];

function EmptyState({ onIntent }: { onIntent: (intent: string) => void }) {
  // The ambient signal is pack-supplied (R3): whichever packs the backend
  // has enabled advertise movers/signals; the library fallback keeps a
  // zero-news corpus alive. No news hook is hardcoded here anymore.
  const { data: telemetry } = useUiTelemetry();
  const { data: graph } = useEntityGraph();
  const { data: articles } = useArticles();
  const { data: clusters } = useClusters();
  const movers = telemetry.movers;
  const signals = telemetry.signals.length
    ? telemetry.signals
    : [
        { label: "DOCS", value: articles.length },
        { label: "CLUSTERS", value: clusters.length },
        { label: "TOPICS MOVING", value: movers.length },
      ];

  return (
    <div className="relative flex-1 overflow-hidden">
      {/* Ambient signal backdrop: the live entity constellation, dimmed. */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 flex items-center justify-center opacity-[0.16]"
        style={{
          maskImage: "radial-gradient(ellipse 70% 65% at 50% 45%, black 30%, transparent 75%)",
          WebkitMaskImage: "radial-gradient(ellipse 70% 65% at 50% 45%, black 30%, transparent 75%)",
        }}
      >
        <div className="w-[820px] max-w-full">
          <EntityGraph data={graph} />
        </div>
      </div>

      <div className="relative z-10 flex h-full flex-col items-center justify-center gap-8 px-6">
        {/* Live pipeline stats — the signal is already flowing. */}
        <div className="flex items-center gap-5 font-mono text-[10.5px] tracking-widest text-muted-foreground">
          <span className="flex items-center gap-2">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" style={{ animation: "blink 2s infinite" }} />
            LIVE SIGNAL
          </span>
          {signals.map((s) => (
            <span key={s.label}>
              {s.value} {s.label}
            </span>
          ))}
        </div>

        {movers.length > 0 ? (
          <div className="w-full max-w-lg">
            <div className="mb-2 text-center font-mono text-[9.5px] tracking-[0.16em] text-muted-foreground/60">
              MOVING NOW — GENERATE A VIEW
            </div>
            <div className="flex flex-col gap-1">
              {movers.map((t, i) => (
                <Button
                  key={t.label}
                  variant="ghost"
                  onClick={() => onIntent(t.intent)}
                  title={`Generate: “${t.intent}”`}
                  className="h-auto w-full justify-start gap-3 px-3 py-2 font-normal"
                >
                  <span className="w-4 shrink-0 text-right font-mono text-[11px] text-muted-foreground/50">
                    {i + 1}
                  </span>
                  <span className="min-w-0 flex-1 truncate text-left text-[13px]">{t.label}</span>
                  {typeof t.change === "number" ? (
                    <span
                      className={
                        "font-mono text-[11px] " + (t.change >= 0 ? "text-emerald-400" : "text-red-400")
                      }
                    >
                      {(t.change >= 0 ? "+" : "") + t.change}%
                    </span>
                  ) : null}
                  <span className="font-mono text-[10px] text-muted-foreground/40">▸ generate</span>
                </Button>
              ))}
            </div>
          </div>
        ) : null}

        <div className="text-center">
          <p className="font-mono text-[11px] text-muted-foreground">
            <kbd className="rounded border border-[#26485a] bg-secondary px-1.5 py-0.5 text-[10px]">⌘K</kbd>{" "}
            describe the view you need — panels assemble to fit
          </p>
          <p className="mt-2 font-mono text-[10.5px] text-muted-foreground/50">
            {QUIET_SUGGESTIONS.map((s, i) => (
              <span key={s}>
                {i > 0 ? " · " : ""}
                <button className="underline-offset-2 hover:text-foreground hover:underline" onClick={() => onIntent(s)}>
                  {s}
                </button>
              </span>
            ))}
          </p>
        </div>
      </div>
    </div>
  );
}

interface Props {
  canvas: CanvasDef;
  onIntent: (intent: string) => void;
}

export default function Canvas({ canvas, onIntent }: Props) {
  const adaptive = useAdaptiveSignals();
  const isSaved = !!canvas.savedId;
  const hasIntent = canvas.intent.trim().length > 0;
  const { spec, source, isLoading } = useUiSpec(canvas.intent, adaptive.signals, hasIntent && !isSaved);

  // M6 in-canvas refinement: an applied refinement overlays the generated spec
  // until the base plan changes (new intent / regenerate), when it is dropped.
  const [refined, setRefined] = useState<UISpec | null>(null);
  useEffect(() => setRefined(null), [spec]);
  const view = refined ?? spec;

  const planner = PLANNER_BADGE[view.generated_by] ?? PLANNER_BADGE.heuristic;

  // A server-persisted canvas renders from its stored spec, not a fresh plan.
  if (isSaved && canvas.savedId) {
    return (
      <div className="flex h-full min-h-0 flex-col">
        <SavedCanvasView savedId={canvas.savedId} />
      </div>
    );
  }

  if (!hasIntent) {
    return (
      <div className="flex h-full min-h-0 flex-col">
        <EmptyState onIntent={onIntent} />
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col px-6 pb-4 pt-5">
      {/* Provenance strip */}
      <div className="mb-3 flex shrink-0 flex-wrap items-center gap-2.5">
        <Badge variant="outline" className={planner.className} title={planner.hint}>
          {planner.label}
        </Badge>
        <Badge variant={isLoading ? "sync" : source === "live" ? "live" : "demo"}>
          {isLoading ? "SYNC" : source === "live" ? "LIVE" : "DEMO"}
        </Badge>
        <span className="font-grotesk text-sm font-semibold">{view.title}</span>
        {view.subtitle ? (
          <span className="font-mono text-[10.5px] text-muted-foreground">{view.subtitle}</span>
        ) : null}
        {refined ? (
          <Badge variant="outline" className="border-violet-400/30 bg-violet-400/10 text-violet-400" title="This canvas has been refined in place">
            REFINED
          </Badge>
        ) : null}
        <span className="flex-1" />
        {adaptive.signals.dismissed.length > 0 ? (
          <span className="font-mono text-[10.5px] text-muted-foreground">
            muted:{" "}
            {adaptive.signals.dismissed.map((t, i) => (
              <button
                key={t}
                onClick={() => adaptive.restore(t)}
                title={`Restore ${PANEL_DEFS[t]?.title ?? t}`}
                className="cursor-pointer line-through hover:text-foreground hover:no-underline"
              >
                {(i > 0 ? ", " : "") + t}
              </button>
            ))}
          </span>
        ) : null}
        {hasSignals(adaptive.signals) ? (
          <Button
            variant="outline"
            size="sm"
            className="h-6 rounded-md px-2 font-mono text-[10px] text-muted-foreground"
            onClick={adaptive.reset}
            title="Forget pins, mutes and interaction weights"
          >
            <RotateCcw className="!size-3" /> RESET
          </Button>
        ) : null}
        {/* Persist / share this canvas server-side (M8). */}
        <CanvasActions spec={view} />
      </div>

      {/* In-canvas refinement (M6): mutate this layout with a follow-up. */}
      <div className="mb-3 shrink-0">
        <RefineBar spec={view} onRefined={setRefined} />
      </div>

      <div className="min-h-0 flex-1">
        <SpecRenderer spec={view} adaptive={adaptive} />
      </div>
    </div>
  );
}
