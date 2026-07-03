// In-canvas refinement input (M6): a follow-up instruction that mutates the
// current canvas in place (add/remove/focus a panel) instead of regenerating
// from a new intent. Applies the diff returned by POST /api/v1/ui/refine.

import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Loader2, Wand2 } from "lucide-react";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { refineUi } from "../lib/refineApi";
import type { UISpec } from "./spec";

export default function RefineBar({
  spec,
  onRefined,
}: {
  spec: UISpec;
  onRefined: (spec: UISpec) => void;
}) {
  const [instruction, setInstruction] = useState("");
  const [note, setNote] = useState<string | null>(null);
  const refine = useMutation({
    mutationFn: (text: string) => refineUi({ spec, instruction: text }),
    onSuccess: (res) => {
      if (res.changed) {
        onRefined(res.spec);
        setInstruction("");
        setNote(null);
      } else {
        setNote("no change — try add / remove / focus on <topic>");
      }
    },
    onError: () => setNote("refinement unavailable"),
  });

  const submit = () => {
    const text = instruction.trim();
    if (text) refine.mutate(text);
  };

  return (
    <div className="flex items-center gap-2">
      <div className="relative flex-1">
        <Wand2 className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground/50" />
        <Input
          value={instruction}
          onChange={(e) => setInstruction(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !refine.isPending) submit();
          }}
          placeholder="refine: add sentiment, remove claims, focus on the delta…"
          className="h-7 pl-8 font-mono text-[11px]"
        />
      </div>
      <Button
        variant="outline"
        size="sm"
        className="h-7 rounded-md px-3 font-mono text-[10px]"
        disabled={refine.isPending || !instruction.trim()}
        onClick={submit}
        title="Apply this refinement to the current canvas"
      >
        {refine.isPending ? <Loader2 className="!size-3 animate-spin" /> : null} REFINE
      </Button>
      {note ? <span className="font-mono text-[10px] text-muted-foreground/70">{note}</span> : null}
    </div>
  );
}
