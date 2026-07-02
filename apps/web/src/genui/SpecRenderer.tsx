// Renders a ui-spec-v1 layout: the plan note as a subtle strip, then a
// 12-column grid of registry panels fitted to the request — spec spans are
// treated as hints and each row is stretched to fill the full width, so a
// focused intent yields a few large panels and a broad one a dense grid.

import { panelComponent } from "./registry";
import type { PanelSpec, UISpec } from "./spec";
import type { AdaptiveSignals } from "./signals";

const MIN = 3;
const MAX = 12;

// Walk the panels row by row and stretch each row's last panel so every row
// fills the 12-column grid — no dangling half-empty rows. Exported for the
// command bar's live ghost preview, which mirrors the real layout.
export function fitSpans(panels: PanelSpec[]): number[] {
  const spans = panels.map((p) => Math.max(MIN, Math.min(MAX, p.span || 6)));
  let i = 0;
  while (i < spans.length) {
    let used = 0;
    const start = i;
    while (i < spans.length && used + spans[i] <= MAX) {
      used += spans[i];
      i += 1;
    }
    if (i > start && used < MAX) spans[i - 1] += MAX - used;
  }
  return spans;
}

interface Props {
  spec: UISpec;
  adaptive: AdaptiveSignals;
}

export default function SpecRenderer({ spec, adaptive }: Props) {
  const note = spec.panels.find((p) => p.type === "note" && p.body);
  const gridPanels = spec.panels.filter((p) => p.type !== "note");
  const spans = fitSpans(gridPanels);

  return (
    <div>
      {note ? (
        <p
          className="mb-4 border-l-2 border-primary/40 pl-3 font-mono text-[11.5px] leading-relaxed text-muted-foreground"
          title={note.rationale || undefined}
        >
          {note.body}
        </p>
      ) : null}
      <div className="grid grid-cols-12 gap-3">
        {gridPanels.map((panel, i) => {
          const Component = panelComponent(panel.type);
          return (
            <div key={panel.id} className="min-w-0" style={{ gridColumn: `span ${spans[i]}` }}>
              <Component
                panel={panel}
                pinned={adaptive.isPinned(panel.type)}
                onPin={() => adaptive.togglePin(panel.type)}
                onDismiss={() => adaptive.dismiss(panel.type)}
                onTouch={() => adaptive.touch(panel.type)}
              />
            </div>
          );
        })}
      </div>
    </div>
  );
}
