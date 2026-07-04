// Renders a server-persisted canvas (M8): a saved canvas fetched by id, or a
// read-only shared canvas fetched by token. Both render the stored ui-spec
// through the same SpecRenderer the generative canvas uses.

import SpecRenderer from "./SpecRenderer";
import { useAdaptiveSignals } from "./signals";
import { useSavedCanvas, useSharedCanvas } from "./useSavedCanvases";
import { Badge } from "../components/ui/badge";

interface Props {
  savedId?: string;
  sharedToken?: string;
}

export default function SavedCanvasView({ savedId, sharedToken }: Props) {
  const adaptive = useAdaptiveSignals();
  const saved = useSavedCanvas(sharedToken ? null : savedId ?? null);
  const shared = useSharedCanvas(sharedToken ?? null);
  const readOnly = !!sharedToken;
  const query = readOnly ? shared : saved;
  const spec = query.data?.spec;

  if (query.isLoading) {
    return (
      <div className="flex h-full items-center justify-center font-mono text-[11px] text-muted-foreground">
        loading canvas…
      </div>
    );
  }

  if (query.isError || !spec) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-1 font-mono text-[11px] text-muted-foreground">
        <span>{readOnly ? "shared canvas not found or link revoked" : "saved canvas unavailable"}</span>
        <span className="text-muted-foreground/50">the persisted-canvas API may be disabled</span>
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col px-6 pb-4 pt-5">
      <div className="mb-3 flex shrink-0 flex-wrap items-center gap-2.5">
        <Badge
          variant="outline"
          className={
            readOnly
              ? "border-sky-400/30 bg-sky-400/10 text-sky-400"
              : "border-emerald-400/30 bg-emerald-400/10 text-emerald-400"
          }
          title={readOnly ? "A read-only shared canvas" : "A saved canvas"}
        >
          {readOnly ? "SHARED · READ-ONLY" : "SAVED"}
        </Badge>
        <span className="font-grotesk text-sm font-semibold">{spec.title}</span>
        {spec.subtitle ? (
          <span className="font-mono text-[10.5px] text-muted-foreground">{spec.subtitle}</span>
        ) : null}
      </div>

      <div className="min-h-0 flex-1">
        <SpecRenderer spec={spec} adaptive={adaptive} />
      </div>
    </div>
  );
}
