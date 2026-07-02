// Chrome shared by every generated panel: shadcn Card with title, provenance
// badge, and the pin / mute controls that feed the adaptive usage signals.

import type { ReactNode } from "react";
import { Pin, X } from "lucide-react";
import { Card } from "../components/ui/card";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { cn } from "../lib/utils";
import type { Source } from "../lib/queries";
import type { PanelSpec } from "./spec";

export interface GenPanelProps {
  panel: PanelSpec;
  pinned: boolean;
  onPin: () => void;
  onDismiss: () => void;
  onTouch: () => void;
  source?: Source;
  isLoading?: boolean;
  children: ReactNode;
}

export default function GenPanel({
  panel,
  pinned,
  onPin,
  onDismiss,
  onTouch,
  source,
  isLoading,
  children,
}: GenPanelProps) {
  // The plan note is meta-content: pinning/muting/weighting it makes no sense.
  const adjustable = panel.type !== "note";
  return (
    <Card
      className="hud-corners flex h-full min-w-0 flex-col transition-shadow duration-300 hover:shadow-[0_0_28px_-10px_hsl(var(--primary)/0.45)]"
      onClick={adjustable ? onTouch : undefined}
      title={panel.rationale || undefined}
    >
      <div className="flex items-center gap-1.5 px-4 pb-2.5 pt-3.5">
        <div className="min-w-0 flex-1 truncate font-grotesk text-[13.5px] font-semibold">
          {panel.title}
        </div>
        {source ? (
          <Badge variant={isLoading ? "sync" : source === "live" ? "live" : "demo"}>
            {isLoading ? "SYNC" : source === "live" ? "LIVE" : "DEMO"}
          </Badge>
        ) : null}
        {adjustable ? (
          <>
            <Button
              variant="ghost"
              size="iconSm"
              className={cn("text-muted-foreground", pinned && "text-primary hover:text-primary")}
              onClick={(e) => {
                e.stopPropagation();
                onPin();
              }}
              title={pinned ? "Unpin — stop always including this panel" : "Pin — always include this panel"}
            >
              <Pin className={cn(pinned && "fill-current")} />
            </Button>
            <Button
              variant="ghost"
              size="iconSm"
              className="text-muted-foreground"
              onClick={(e) => {
                e.stopPropagation();
                onDismiss();
              }}
              title="Mute — hide this panel type from future canvases"
            >
              <X />
            </Button>
          </>
        ) : null}
      </div>
      <div className="min-h-0 flex-1 px-4 pb-4">{children}</div>
    </Card>
  );
}
