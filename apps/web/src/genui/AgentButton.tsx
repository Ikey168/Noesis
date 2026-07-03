// Agent launcher (M10): a sidebar entry point that runs the analyst or
// investigator agent on a goal over the MCP surface, shows the run summary
// (KG, findings, steps, review-gate status), and drops the agent's produced
// canvas onto the board. Disabled unless the backend has NOESIS_AGENT_API on.

import { useState } from "react";
import { Bot, Check, ListTree, Loader2, Play, Radio, Search, X } from "lucide-react";
import { Button } from "../components/ui/button";
import { Badge } from "../components/ui/badge";
import { Input } from "../components/ui/input";
import { canvasApi } from "../lib/canvasStore";
import type { AnalystResult, InvestigatorResult } from "../lib/agentApi";
import { useAgentStatus, useRunAnalyst, useRunInvestigator, useRunReplay } from "./useAgents";

type Mode = "analyst" | "investigator";

function isInvestigation(r: AnalystResult | InvestigatorResult): r is InvestigatorResult {
  return "gated_calls" in r;
}

const PLANE_COLOR: Record<string, string> = {
  provisioning: "text-violet-400",
  osint: "text-teal-400",
  genui: "text-amber-400",
};

// The audit trail of a run, replayed from GET /agent/runs/{id} on demand.
function RunTrail({ runId }: { runId: string }) {
  const [open, setOpen] = useState(false);
  const trail = useRunReplay(runId, open);
  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 font-mono text-[10px] text-muted-foreground hover:text-foreground"
      >
        <ListTree className="size-3" /> {open ? "hide trail" : "view trail"}
      </button>
      {open ? (
        <div className="mt-2 flex flex-col gap-0.5">
          {trail.isLoading ? (
            <span className="font-mono text-[10px] text-muted-foreground">loading trail…</span>
          ) : trail.data && trail.data.calls.length ? (
            trail.data.calls.map((c) => (
              <div key={c.step} className="flex items-center gap-2 font-mono text-[10px]">
                <span className="w-5 text-right text-muted-foreground/50">{c.step}</span>
                <span className={"w-20 shrink-0 " + (PLANE_COLOR[c.plane] ?? "text-muted-foreground")}>
                  {c.plane}
                </span>
                <span className="min-w-0 flex-1 truncate text-foreground/80">{c.tool}</span>
                <span className={c.ok ? "text-emerald-400" : "text-red-400"}>{c.ok ? "ok" : "err"}</span>
              </div>
            ))
          ) : (
            <span className="font-mono text-[10px] text-muted-foreground">no trail recorded</span>
          )}
        </div>
      ) : null}
    </div>
  );
}

function ResultCard({
  result,
  onOpenCanvas,
}: {
  result: AnalystResult | InvestigatorResult;
  onOpenCanvas: () => Promise<void>;
}) {
  const findings = isInvestigation(result) ? result.surface : result.osint;
  const [opening, setOpening] = useState(false);
  const [opened, setOpened] = useState(false);
  return (
    <div className="mt-4 flex flex-col gap-2.5 rounded-lg border bg-secondary/30 px-3.5 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="live">DONE</Badge>
        <span className="font-mono text-[11px] text-muted-foreground">
          KG {result.kg.name} {result.kg.provisioned ? "(provisioned)" : "(reused)"}
        </span>
        <span className="flex-1" />
        <span className="font-mono text-[10.5px] text-muted-foreground/70">{result.steps} steps</span>
        <span className="font-mono text-[10.5px] text-muted-foreground/70">{result.findings} findings</span>
        {isInvestigation(result) ? (
          <Badge
            variant="outline"
            className={
              result.gated_calls === 0
                ? "border-emerald-400/30 bg-emerald-400/10 text-emerald-400"
                : "border-red-400/30 bg-red-400/10 text-red-400"
            }
            title="Gated OSINT tools invoked (must be 0 while the review gate is off)"
          >
            {result.gated_calls === 0 ? "GATE RESPECTED" : `GATED x${result.gated_calls}`}
          </Badge>
        ) : null}
      </div>

      <div className="flex flex-wrap gap-1.5">
        {findings.map((f, i) => (
          <span
            key={`${f.tool}-${i}`}
            className={
              "rounded border px-1.5 py-0.5 font-mono text-[9.5px] " +
              (f.ok
                ? "border-teal-400/30 bg-teal-400/10 text-teal-400"
                : "border-muted-foreground/20 bg-secondary text-muted-foreground/60")
            }
          >
            {f.tool}
          </span>
        ))}
      </div>

      <Button
        size="sm"
        className="h-7 self-start rounded-md px-3 font-mono text-[10.5px]"
        disabled={opening || opened}
        onClick={() => {
          setOpening(true);
          onOpenCanvas()
            .then(() => setOpened(true))
            .catch(() => undefined)
            .finally(() => setOpening(false));
        }}
        title="Save the agent's canvas and open it on the board"
      >
        {opened ? <Check className="!size-3" /> : opening ? <Loader2 className="!size-3 animate-spin" /> : null}
        {opened ? "OPENED" : "OPEN CANVAS"}
      </Button>

      {/* The full audit trail of this run (M10.4), replayed on demand. */}
      <RunTrail runId={result.run_id} />
    </div>
  );
}

function AgentModal({
  onClose,
  onOpenSaved,
}: {
  onClose: () => void;
  onOpenSaved: (savedId: string, label: string) => void;
}) {
  const status = useAgentStatus();
  const analyst = useRunAnalyst();
  const investigator = useRunInvestigator();
  const [mode, setMode] = useState<Mode>("analyst");
  const [goal, setGoal] = useState("");
  const [claimId, setClaimId] = useState("");
  const [entity, setEntity] = useState("");
  const [topic, setTopic] = useState("");
  const [live, setLive] = useState(false);

  const enabled = status.data?.enabled ?? false;
  const running = analyst.isPending || investigator.isPending;
  const result = mode === "analyst" ? analyst.data : investigator.data;
  const error = mode === "analyst" ? analyst.error : investigator.error;
  const transport = live ? "live" : "local";

  const run = () => {
    if (!goal.trim()) return;
    if (mode === "analyst") {
      analyst.mutate({
        goal: goal.trim(),
        claim_id: claimId.trim() || undefined,
        entity: entity.trim() || undefined,
        topic: topic.trim() || undefined,
        transport,
      });
    } else {
      investigator.mutate({
        title: goal.trim(),
        entities: entity.trim() ? [entity.trim()] : undefined,
        topic: topic.trim() || undefined,
        transport,
        claim_id: claimId.trim() || undefined,
      });
    }
  };

  const openCanvas = async () => {
    if (!result) return;
    // Persist the agent's canvas, then open the saved copy on the board.
    const canvas = await canvasApi.save({ spec: result.canvas, title: result.canvas.title });
    onOpenSaved(canvas.id, canvas.title);
    onClose();
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-6" onClick={onClose}>
      <div
        className="flex max-h-[82vh] w-full max-w-xl flex-col overflow-hidden rounded-xl border bg-[#070d13] shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2.5 border-b px-5 py-3.5">
          <Bot className="size-4 text-primary" />
          <span className="font-grotesk text-sm font-semibold">Run an agent</span>
          {!enabled ? (
            <Badge variant="outline" className="border-amber-400/30 bg-amber-400/10 text-amber-400" title="Set NOESIS_AGENT_API=on">
              DISABLED
            </Badge>
          ) : null}
          <span className="flex-1" />
          <button onClick={onClose} className="rounded p-1 text-muted-foreground hover:bg-secondary hover:text-foreground">
            <X className="size-4" />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          <div className="mb-3 flex items-center gap-1.5">
            {(["analyst", "investigator"] as Mode[]).map((m) => (
              <Button
                key={m}
                variant={mode === m ? "default" : "outline"}
                size="sm"
                className="h-7 rounded-md px-3 font-mono text-[10.5px]"
                onClick={() => setMode(m)}
              >
                {m === "analyst" ? <Play className="!size-3" /> : <Search className="!size-3" />}
                {m.toUpperCase()}
              </Button>
            ))}
            <span className="flex-1" />
            {/* Transport: in-process backend (default) vs the live MCP surface. */}
            <Button
              variant={live ? "default" : "outline"}
              size="sm"
              className="h-7 rounded-md px-3 font-mono text-[10.5px]"
              onClick={() => setLive((v) => !v)}
              title="Drive the run over the live MCP servers instead of the in-process backend"
            >
              <Radio className="!size-3" /> {live ? "LIVE MCP" : "IN-PROCESS"}
            </Button>
          </div>

          <label className="mb-1 block font-mono text-[9.5px] tracking-[0.16em] text-muted-foreground/60">
            {mode === "analyst" ? "GOAL" : "INVESTIGATION TITLE"}
          </label>
          <Input
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
            placeholder={mode === "analyst" ? "flooding in the coastal delta" : "delta flooding response"}
            onKeyDown={(e) => {
              if (e.key === "Enter" && enabled && !running) run();
            }}
          />

          <div className="mt-2 grid grid-cols-3 gap-2">
            <Input value={entity} onChange={(e) => setEntity(e.target.value)} placeholder="entity (optional)" />
            <Input value={topic} onChange={(e) => setTopic(e.target.value)} placeholder="topic (optional)" />
            <Input value={claimId} onChange={(e) => setClaimId(e.target.value)} placeholder="claim id (optional)" />
          </div>

          <Button
            className="mt-3 h-8 rounded-md px-4 font-mono text-[11px]"
            disabled={!enabled || running || !goal.trim()}
            onClick={run}
            title={enabled ? "Run the agent" : "The agent API is disabled on the backend"}
          >
            {running ? <Loader2 className="!size-3.5 animate-spin" /> : <Play className="!size-3.5" />}
            {running ? "RUNNING…" : "RUN"}
          </Button>

          {error ? (
            <div className="mt-3 font-mono text-[10.5px] text-red-400">the agent run failed</div>
          ) : null}
          {result ? <ResultCard result={result} onOpenCanvas={openCanvas} /> : null}
        </div>
      </div>
    </div>
  );
}

export default function AgentButton({
  onOpenSaved,
}: {
  onOpenSaved: (savedId: string, label: string) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button
        variant="outline"
        size="sm"
        className="h-7 w-full justify-start gap-2 rounded-md px-2.5 font-mono text-[10.5px] text-muted-foreground"
        onClick={() => setOpen(true)}
        title="Run the analyst or investigator agent"
      >
        <Bot className="!size-3.5" /> RUN AGENT
      </Button>
      {open ? <AgentModal onClose={() => setOpen(false)} onOpenSaved={onOpenSaved} /> : null}
    </>
  );
}
