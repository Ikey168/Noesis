// The command bar — the app's single control surface, living in the top
// bar (⌘K to focus). It is not a chat box: the local planner runs on every
// keystroke, so the bar shows its parse of your intent (facets, topic,
// window, source type) and a ghost of the layout it will build, live,
// before you commit with ⏎.

import { useEffect, useMemo, useRef, useState } from "react";
import { CornerDownLeft, ScanSearch } from "lucide-react";
import { localPlan } from "./planner";
import { fitSpans } from "./SpecRenderer";
import { loadSignals } from "./signals";
import { cn } from "../lib/utils";

interface Props {
  onIntent: (intent: string) => void;
}

const FACET_CHIP = "rounded border border-teal-400/30 bg-teal-400/10 px-1.5 py-px text-teal-400";
const TOPIC_CHIP = "rounded border border-amber-400/30 bg-amber-400/10 px-1.5 py-px text-amber-400";
const WINDOW_CHIP = "rounded border border-sky-400/30 bg-sky-400/10 px-1.5 py-px text-sky-400";
const TYPE_CHIP = "rounded border border-violet-400/30 bg-violet-400/10 px-1.5 py-px text-violet-400";

export default function CommandBar({ onIntent }: Props) {
  const [draft, setDraft] = useState("");
  const [focused, setFocused] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  // ⌘K / Ctrl-K focuses the bar from anywhere; Escape clears and blurs.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        inputRef.current?.focus();
        inputRef.current?.select();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Live plan: the client planner is deterministic and instant, so the
  // preview is exact for offline planning and a faithful approximation of
  // the backend plan (same rules, minus data-availability drops).
  const preview = useMemo(() => {
    const trimmed = draft.trim();
    if (!trimmed) return null;
    const spec = localPlan(trimmed, loadSignals());
    const panels = spec.panels.filter((p) => p.type !== "note");
    return { spec, panels, spans: fitSpans(panels) };
  }, [draft]);

  const submit = () => {
    if (!draft.trim()) return;
    onIntent(draft);
    setDraft("");
    inputRef.current?.blur();
  };

  return (
    <div className="relative min-w-0 max-w-xl flex-1">
      <div
        className={cn(
          "flex items-center gap-2.5 rounded-lg border border-input bg-[#0a141d] px-3 py-2 transition-all",
          focused && "border-primary/60 shadow-[0_0_22px_-6px_hsl(var(--primary)/0.5)]",
        )}
      >
        <ScanSearch className="size-4 shrink-0 text-muted-foreground/60" />
        <input
          ref={inputRef}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
          onKeyDown={(e) => {
            if (e.key === "Enter") submit();
            if (e.key === "Escape") {
              setDraft("");
              inputRef.current?.blur();
            }
          }}
          placeholder="Describe the view you need…"
          maxLength={500}
          className="min-w-0 flex-1 border-0 bg-transparent font-mono text-[12.5px] text-foreground outline-none placeholder:font-sans placeholder:text-[13px] placeholder:text-muted-foreground/60"
        />
        {draft.trim() ? (
          <span className="flex shrink-0 items-center gap-1 font-mono text-[9.5px] text-muted-foreground/60">
            <CornerDownLeft className="size-3" /> generate
          </span>
        ) : (
          <span className="shrink-0 rounded border border-[#26485a] px-1.5 py-px font-mono text-[9.5px] text-muted-foreground/50">
            ⌘K
          </span>
        )}
      </div>

      {/* Live plan preview — informational only, never captures the pointer. */}
      {focused && preview ? (
        <div className="pointer-events-none absolute left-0 right-0 top-full z-50 mt-2 rounded-lg border bg-card/95 p-3 shadow-2xl backdrop-blur">
          <div className="mb-2 flex flex-wrap items-center gap-1.5 font-mono text-[10px]">
            <span className="mr-0.5 tracking-widest text-muted-foreground/60">PLAN</span>
            {(preview.spec.facets ?? []).map((f) => (
              <span key={f} className={FACET_CHIP}>
                {f}
              </span>
            ))}
            {preview.spec.topic ? <span className={TOPIC_CHIP}>“{preview.spec.topic}”</span> : null}
            {preview.panels.some((p) => typeof p.params?.days === "number") ? (
              <span className={WINDOW_CHIP}>
                {String(preview.panels.find((p) => typeof p.params?.days === "number")?.params?.days)}d window
              </span>
            ) : null}
            {preview.spec.source_type ? <span className={TYPE_CHIP}>{preview.spec.source_type}</span> : null}
            <span className="ml-auto text-muted-foreground/60">
              {preview.panels.length} panel{preview.panels.length === 1 ? "" : "s"}
            </span>
          </div>
          <div className="grid grid-cols-12 gap-1">
            {preview.panels.map((p, i) => (
              <div
                key={p.id}
                style={{ gridColumn: `span ${preview.spans[i]}` }}
                className="flex h-10 items-center justify-center overflow-hidden rounded border border-dashed border-border bg-secondary/40 px-1"
              >
                <span className="truncate font-mono text-[9.5px] text-muted-foreground">{p.title}</span>
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}
