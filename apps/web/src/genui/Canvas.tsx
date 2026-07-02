// The generative canvas — the app's only surface. An empty canvas is not a
// chat waiting for input: it shows the live signal already flowing through
// the pipeline (entity constellation, movers, ingest stats) and points at
// the ⌘K command bar. With an intent, the planned layout fills the surface.

import { RotateCcw } from "lucide-react";
import SpecRenderer from "./SpecRenderer";
import EntityGraph from "../components/charts/EntityGraph";
import { useArticles, useClusters, useEntityGraph, useTrending, usePackStatus } from "../lib/queries";
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
  const { newsPack } = usePackStatus();
  const { data: graph } = useEntityGraph();
  const { data: trending } = useTrending();
  const { data: articles } = useArticles();
  const { data: clusters } = useClusters();
  const movers = trending.slice(0, 5);

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
          <span>{articles.length} DOCS</span>
          <span>{clusters.length} CLUSTERS</span>
          <span>{trending.length} TOPICS MOVING</span>
        </div>

        {newsPack && movers.length > 0 ? (
          <div className="w-full max-w-lg">
            <div className="mb-2 text-center font-mono text-[9.5px] tracking-[0.16em] text-muted-foreground/60">
              MOVING NOW — GENERATE A COVERAGE VIEW
            </div>
            <div className="flex flex-col gap-1">
              {movers.map((t, i) => (
                <Button
                  key={t.topic}
                  variant="ghost"
                  onClick={() => onIntent(`coverage of ${t.topic.toLowerCase()}`)}
                  title={`Generate: “coverage of ${t.topic.toLowerCase()}”`}
                  className="h-auto w-full justify-start gap-3 px-3 py-2 font-normal"
                >
                  <span className="w-4 shrink-0 text-right font-mono text-[11px] text-muted-foreground/50">
                    {i + 1}
                  </span>
                  <span className="min-w-0 flex-1 truncate text-left text-[13px]">{t.topic}</span>
                  <span
                    className={
                      "font-mono text-[11px] " + (t.change >= 0 ? "text-emerald-400" : "text-red-400")
                    }
                  >
                    {(t.change >= 0 ? "+" : "") + t.change}%
                  </span>
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
  const hasIntent = canvas.intent.trim().length > 0;
  const { spec, source, isLoading } = useUiSpec(canvas.intent, adaptive.signals, hasIntent);

  const planner = PLANNER_BADGE[spec.generated_by] ?? PLANNER_BADGE.heuristic;

  if (!hasIntent) {
    return (
      <div className="flex h-full min-h-0 flex-col">
        <EmptyState onIntent={onIntent} />
      </div>
    );
  }

  return (
    <div className="h-full min-h-0 overflow-y-auto px-6 pb-6 pt-5">
      {/* Provenance strip */}
      <div className="mb-4 flex flex-wrap items-center gap-2.5">
        <Badge variant="outline" className={planner.className} title={planner.hint}>
          {planner.label}
        </Badge>
        <Badge variant={isLoading ? "sync" : source === "live" ? "live" : "demo"}>
          {isLoading ? "SYNC" : source === "live" ? "LIVE" : "DEMO"}
        </Badge>
        <span className="font-grotesk text-sm font-semibold">{spec.title}</span>
        {spec.subtitle ? (
          <span className="font-mono text-[10.5px] text-muted-foreground">{spec.subtitle}</span>
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
      </div>

      <SpecRenderer spec={spec} adaptive={adaptive} />
    </div>
  );
}
