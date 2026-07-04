// Renders a ui-spec-v1 layout as a masonry board: the plan note as a subtle
// strip, then diverse-sized panels that fill the available height in columns
// and expand to the right once a column is full. The board scrolls
// horizontally, so a busy canvas grows sideways instead of into an endless
// vertical scroll. Spec spans are treated as width hints per panel.

import { panelComponent } from "./registry";
import type { PanelSpec, UISpec } from "./spec";
import type { AdaptiveSignals } from "./signals";

const MIN = 3;
const MAX = 12;

// Balance a row of spec spans to fill the 12-column grid. No longer used for
// the main canvas layout (now a masonry column-flow), but kept for the command
// bar's compact plan preview, which sketches the plan as a 12-column tile grid.
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

// Map a spec span (1..12) to a panel width in pixels so panels keep diverse
// sizes in the masonry. Clamped to a comfortable card range.
const COL_MIN = 300;
const COL_MAX = 560;
export function widthFor(span: number): number {
  const s = Math.max(MIN, Math.min(MAX, span || 6));
  return Math.round(Math.max(COL_MIN, Math.min(COL_MAX, 200 + s * 30)));
}

interface Props {
  spec: UISpec;
  adaptive: AdaptiveSignals;
}

export default function SpecRenderer({ spec, adaptive }: Props) {
  const note = spec.panels.find((p) => p.type === "note" && p.body);
  const panels = spec.panels.filter((p) => p.type !== "note");

  return (
    <div className="flex h-full min-h-0 flex-col">
      {note ? (
        <p
          className="mb-3 shrink-0 border-l-2 border-primary/40 pl-3 font-mono text-[11.5px] leading-relaxed text-muted-foreground"
          title={note.rationale || undefined}
        >
          {note.body}
        </p>
      ) : null}
      {/* Masonry board: panels stack top-to-bottom to fill the height, then a
          new column starts on the right; the board scrolls horizontally. */}
      <div className="min-h-0 flex-1 overflow-x-auto overflow-y-hidden pb-1">
        <div className="flex h-full w-max flex-col flex-wrap content-start items-start gap-3">
          {panels.map((panel) => {
            const Component = panelComponent(panel.type);
            return (
              <div key={panel.id} className="min-w-0" style={{ width: widthFor(panel.span) }}>
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
    </div>
  );
}
