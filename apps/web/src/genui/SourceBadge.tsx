// Provenance indicator shared by the canvas header and every panel. Live and
// proxied (MCP) data get a prominent green pill; the sample-data default recedes
// to a quiet dotted marker so a fixtures-only canvas does not read as a mockup.

import { Badge } from "../components/ui/badge";
import type { Source } from "../lib/queries";

interface Props {
  source?: Source;
  isLoading?: boolean;
}

export default function SourceBadge({ source, isLoading }: Props) {
  if (isLoading) return <Badge variant="sync">SYNC</Badge>;
  if (source === "live" || source === "mcp") {
    return <Badge variant="live">{source === "mcp" ? "MCP" : "LIVE"}</Badge>;
  }
  return (
    <Badge variant="demo" title="Sample data. Connect a live source to populate this panel.">
      <span className="h-1.5 w-1.5 rounded-full bg-current opacity-60" aria-hidden="true" />
      SAMPLE
    </Badge>
  );
}
