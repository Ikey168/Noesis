// The sidebar is only a canvas manager: the brand, the open canvases (plus
// the always-present empty "New canvas"), and pipeline status. Everything
// else is generated — intents come from the composer, and suggestions live
// on the empty canvas itself.

import { Plus, Sparkles, X } from "lucide-react";
import { HOME, type CanvasDef } from "../genui/canvases";
import { Button } from "./ui/button";
import { cn } from "../lib/utils";

interface Props {
  canvases: CanvasDef[];
  activeId: string;
  onSelect: (id: string) => void;
  onRemove: (id: string) => void;
  ingestRate: string;
}

export default function Sidebar({ canvases, activeId, onSelect, onRemove, ingestRate }: Props) {
  return (
    <aside className="flex w-60 shrink-0 flex-col border-r bg-[#070d13]">
      {/* Brand */}
      <div className="flex items-center gap-3 border-b px-4 py-4">
        <div
          className="flex h-[34px] w-[34px] shrink-0 items-center justify-center rounded-lg bg-primary shadow-[0_0_22px_-2px_hsl(var(--primary)/0.65)]"
          style={{ animation: "flicker 7s infinite" }}
        >
          <span className="font-grotesk text-[19px] font-bold text-primary-foreground">N</span>
        </div>
        <div className="leading-tight">
          <div className="glow-text font-grotesk text-base font-bold tracking-tight">Noesis</div>
          <div className="mt-0.5 font-mono text-[9.5px] tracking-[0.16em] text-muted-foreground/60">
            GENERATIVE CANVAS
          </div>
        </div>
      </div>

      <nav className="flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto p-2.5">
        <div className="px-2.5 pb-1.5 pt-2 font-mono text-[9.5px] tracking-[0.16em] text-muted-foreground/60">
          CANVASES
        </div>
        {canvases.map((c) => (
          <Button
            key={c.id}
            variant="ghost"
            onClick={() => onSelect(c.id)}
            title={c.intent || "Empty canvas — describe what you want to see"}
            className={cn(
              "h-auto w-full justify-start gap-2.5 px-2.5 py-2 text-[13px] font-medium text-muted-foreground",
              c.id === activeId && "bg-primary/10 text-primary hover:bg-primary/10 hover:text-primary",
            )}
          >
            <span className="w-[18px] shrink-0 text-center">
              {c.id === HOME.id ? <Plus className="size-3.5" /> : <Sparkles className="size-3.5" />}
            </span>
            <span className="min-w-0 flex-1 truncate text-left">{c.label}</span>
            {c.id !== HOME.id ? (
              <span
                role="button"
                title="Close canvas"
                className="rounded p-0.5 text-muted-foreground/50 hover:bg-secondary hover:text-foreground"
                onClick={(e) => {
                  e.stopPropagation();
                  onRemove(c.id);
                }}
              >
                <X className="size-3" />
              </span>
            ) : null}
          </Button>
        ))}
      </nav>

      {/* Footer */}
      <div className="flex flex-col gap-2 border-t px-4 py-3.5">
        <div className="flex items-center gap-2">
          <span className="h-[7px] w-[7px] rounded-full bg-emerald-400" style={{ animation: "blink 2s infinite" }} />
          <span className="font-mono text-[10.5px] text-muted-foreground">PIPELINE LIVE</span>
        </div>
        <div className="flex justify-between font-mono text-[10.5px] text-muted-foreground/60">
          <span>142 sources</span>
          <span>{ingestRate}/min</span>
        </div>
      </div>
    </aside>
  );
}
