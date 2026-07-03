// Save / share actions for a generated canvas (M8). "Save" persists the
// current ui-spec server-side; "Share" mints a read-only link (saving first
// if needed) and copies it to the clipboard.

import { useState } from "react";
import { Check, Copy, Save, Share2 } from "lucide-react";
import { Button } from "../components/ui/button";
import { useSaveCanvas, useShareCanvas } from "./useSavedCanvases";
import type { UISpec } from "./spec";

export default function CanvasActions({ spec }: { spec: UISpec }) {
  const save = useSaveCanvas();
  const share = useShareCanvas();
  const [savedId, setSavedId] = useState<string | null>(null);
  const [shareUrl, setShareUrl] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const ensureSaved = async (): Promise<string> => {
    if (savedId) {
      await save.mutateAsync({ spec, title: spec.title, id: savedId });
      return savedId;
    }
    const canvas = await save.mutateAsync({ spec, title: spec.title });
    setSavedId(canvas.id);
    return canvas.id;
  };

  const onSave = () => {
    ensureSaved().catch(() => undefined);
  };

  const onShare = () => {
    (async () => {
      const id = await ensureSaved();
      const link = await share.mutateAsync(id);
      setShareUrl(link.url);
      try {
        await navigator.clipboard.writeText(link.url);
        setCopied(true);
        setTimeout(() => setCopied(false), 1600);
      } catch {
        // Clipboard unavailable; the link is still shown for manual copy.
      }
    })().catch(() => undefined);
  };

  const busy = save.isPending || share.isPending;

  return (
    <div className="flex items-center gap-1.5">
      <Button
        variant="outline"
        size="sm"
        className="h-6 rounded-md px-2 font-mono text-[10px]"
        onClick={onSave}
        disabled={busy}
        title="Save this canvas to your account"
      >
        {savedId ? <Check className="!size-3" /> : <Save className="!size-3" />}
        {savedId ? "SAVED" : "SAVE"}
      </Button>
      <Button
        variant="outline"
        size="sm"
        className="h-6 rounded-md px-2 font-mono text-[10px]"
        onClick={onShare}
        disabled={busy}
        title="Create a read-only share link"
      >
        <Share2 className="!size-3" /> SHARE
      </Button>
      {shareUrl ? (
        <button
          type="button"
          onClick={() => {
            navigator.clipboard?.writeText(shareUrl).then(
              () => {
                setCopied(true);
                setTimeout(() => setCopied(false), 1600);
              },
              () => undefined,
            );
          }}
          title={shareUrl}
          className="flex items-center gap-1 font-mono text-[10px] text-muted-foreground hover:text-foreground"
        >
          {copied ? <Check className="size-3 text-emerald-400" /> : <Copy className="size-3" />}
          {copied ? "link copied" : "copy link"}
        </button>
      ) : null}
    </div>
  );
}
