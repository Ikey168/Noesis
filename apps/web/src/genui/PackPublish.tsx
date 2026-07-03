// Author and publish a domain pack (M9): paste a noesis-pack-v1 manifest and
// publish it to the registry via POST /api/v1/packs/publish. Client-side JSON
// parsing gives immediate feedback; the backend validates the contract and
// returns the published coordinates. Disabled unless pack admin is enabled.

import { useState } from "react";
import { Check, ChevronDown, ChevronRight, Loader2, Upload } from "lucide-react";
import { Button } from "../components/ui/button";
import { usePublishPack } from "./usePacks";

const TEMPLATE = `{
  "pack_format": "noesis-pack-v1",
  "name": "energy",
  "version": "1.0.1",
  "description": "Energy-sector pack",
  "source_types": ["news"],
  "ui_flags": { "energy": true },
  "planner_keywords": { "trend": ["grid", "outage"] }
}`;

export default function PackPublish({ admin }: { admin: boolean }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState(TEMPLATE);
  const [parseError, setParseError] = useState<string | null>(null);
  const [published, setPublished] = useState<string | null>(null);
  const publish = usePublishPack();

  const submit = () => {
    setPublished(null);
    let manifest: Record<string, unknown>;
    try {
      manifest = JSON.parse(text);
    } catch (err) {
      setParseError(err instanceof Error ? err.message : "invalid JSON");
      return;
    }
    setParseError(null);
    publish.mutate(
      { manifest, force: false },
      { onSuccess: (p) => setPublished(`${p.name} v${p.version}`) },
    );
  };

  return (
    <div className="mt-5 border-t pt-4">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 font-mono text-[9.5px] tracking-[0.16em] text-muted-foreground/60 hover:text-foreground"
      >
        {open ? <ChevronDown className="size-3" /> : <ChevronRight className="size-3" />}
        AUTHOR &amp; PUBLISH A PACK
      </button>
      {open ? (
        <div className="mt-2.5 flex flex-col gap-2">
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            spellCheck={false}
            rows={10}
            className="w-full rounded-md border border-input bg-secondary/40 px-3 py-2 font-mono text-[11px] leading-relaxed text-foreground outline-none focus:border-primary/50"
            placeholder="paste a noesis-pack-v1 manifest…"
          />
          <div className="flex items-center gap-2">
            <Button
              size="sm"
              className="h-7 rounded-md px-3 font-mono text-[10px]"
              disabled={!admin || publish.isPending}
              onClick={submit}
              title={admin ? "Publish this manifest to the registry" : "Enable NOESIS_PACKS_ADMIN to publish"}
            >
              {publish.isPending ? <Loader2 className="!size-3 animate-spin" /> : <Upload className="!size-3" />}
              PUBLISH
            </Button>
            {published ? (
              <span className="flex items-center gap-1 font-mono text-[10px] text-emerald-400">
                <Check className="size-3" /> published {published}
              </span>
            ) : null}
            {parseError ? <span className="font-mono text-[10px] text-red-400">JSON: {parseError}</span> : null}
            {publish.isError && !parseError ? (
              <span className="font-mono text-[10px] text-red-400">
                rejected — invalid manifest or version already published
              </span>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}
