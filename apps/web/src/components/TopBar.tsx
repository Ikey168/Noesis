import { useEffect, useState } from "react";
import { useBackendStatus, type BackendStatus } from "../lib/queries";
import CommandBar from "../genui/CommandBar";
import { cn } from "../lib/utils";

const STATUS_META: Record<BackendStatus, { className: string; dot: string; label: string; hint: string }> = {
  checking: {
    className: "border-amber-400/30 bg-amber-400/10 text-amber-400",
    dot: "bg-amber-400",
    label: "CONNECTING",
    hint: "Probing the backend…",
  },
  online: {
    className: "border-emerald-400/30 bg-emerald-400/10 text-emerald-400",
    dot: "bg-emerald-400 shadow-[0_0_6px_currentColor]",
    label: "BACKEND LIVE",
    hint: "Connected to the Noesis API",
  },
  offline: {
    className: "border-slate-500/30 bg-slate-500/10 text-slate-400",
    dot: "bg-slate-400",
    label: "DEMO MODE",
    hint: "Backend unreachable — panels fall back to the demo dataset",
  },
};

function ConnectionPill() {
  const status = useBackendStatus();
  const meta = STATUS_META[status];
  return (
    <div
      title={meta.hint}
      className={cn(
        "inline-flex items-center gap-2 rounded-md border px-2.5 py-1.5 font-mono text-[10px] tracking-widest",
        meta.className,
      )}
    >
      <span
        className={cn("h-[7px] w-[7px] rounded-full", meta.dot)}
        style={status === "checking" ? { animation: "pulse 1s ease-in-out infinite" } : undefined}
      />
      {meta.label}
    </div>
  );
}

function useUtcClock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);
  return now;
}

interface Props {
  onIntent: (intent: string) => void;
}

export default function TopBar({ onIntent }: Props) {
  const now = useUtcClock();
  const pad2 = (x: number) => String(x).padStart(2, "0");
  const clock = `${pad2(now.getUTCHours())}:${pad2(now.getUTCMinutes())}:${pad2(now.getUTCSeconds())}`;
  const dateStr = now
    .toLocaleDateString("en-US", { month: "short", day: "2-digit", timeZone: "UTC" })
    .toUpperCase();

  return (
    <header className="flex h-[54px] shrink-0 items-center gap-4 border-b bg-[#070d13] px-4">
      <CommandBar onIntent={onIntent} />
      <div className="flex-1" />
      <div className="flex items-center gap-4">
        <ConnectionPill />
        <div className="text-right leading-tight">
          <div className="font-mono text-sm font-medium text-primary">{clock}</div>
          <div className="font-mono text-[9px] tracking-widest text-muted-foreground/60">UTC · {dateStr}</div>
        </div>
        <div className="flex h-8 w-8 items-center justify-center rounded-full border border-[#26485a] bg-secondary font-mono text-[11px] text-muted-foreground">
          AK
        </div>
      </div>
    </header>
  );
}
