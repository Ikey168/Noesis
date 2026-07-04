// Sankey — a layered flow diagram. Nodes are stacked per layer with height
// proportional to the flow through them; links are ribbons whose thickness is
// the flow value. A link takes its color from its source node, so a first-layer
// category (e.g. a source type) is followed by eye through the diagram; the
// legend maps those colors. Node bars and labels are crisp HTML overlays; only
// the ribbons live in the SVG (their fills tolerate the horizontal stretch that
// lets the diagram fill the panel width).

import { ACCENT, palette, colors, fonts } from "../../theme";

export interface SankeyNode {
  id: string;
  layer: number;
  label: string;
}
export interface SankeyLink {
  source: string;
  target: string;
  value: number;
}

// Categorical hues for the first layer, in fixed order (validated CVD-safe).
const CAT = [ACCENT, palette.violet, palette.amber, palette.blue, palette.teal, palette.pos];
const NODE_W = 7; // px width of a node bar
const GAP = 6; // px vertical gap between stacked nodes
const PAD_Y = 4; // px top/bottom padding

export default function Sankey({
  nodes,
  links,
  height = 210,
}: {
  nodes: SankeyNode[];
  links: SankeyLink[];
  height?: number;
}) {
  if (!nodes.length || !links.length) {
    return (
      <div style={{ height, display: "flex", alignItems: "center", justifyContent: "center", color: palette.faint, fontFamily: fonts.mono, fontSize: 11.5 }}>
        no flow to chart
      </div>
    );
  }

  const layerNums = Array.from(new Set(nodes.map((n) => n.layer))).sort((a, b) => a - b);
  const layerIndex = new Map(layerNums.map((l, i) => [l, i]));
  const nLayers = layerNums.length;
  const byId = new Map(nodes.map((n) => [n.id, n]));

  // Node value = max(incoming, outgoing) so flow-conserving nodes are exact.
  const outSum = new Map<string, number>();
  const inSum = new Map<string, number>();
  for (const l of links) {
    outSum.set(l.source, (outSum.get(l.source) ?? 0) + l.value);
    inSum.set(l.target, (inSum.get(l.target) ?? 0) + l.value);
  }
  const value = (id: string) => Math.max(outSum.get(id) ?? 0, inSum.get(id) ?? 0);

  // Nodes per layer, and the tallest layer's total value -> px-per-unit scale.
  const perLayer = layerNums.map((l) => nodes.filter((n) => n.layer === l));
  let maxUnits = 1;
  perLayer.forEach((ns) => {
    maxUnits = Math.max(maxUnits, ns.reduce((s, n) => s + value(n.id), 0));
  });
  const maxNodes = Math.max(...perLayer.map((ns) => ns.length));
  const scale = (height - 2 * PAD_Y - (maxNodes - 1) * GAP) / maxUnits;

  // Colors: first-layer nodes get categorical hues; later nodes are muted.
  const nodeColor = new Map<string, string>();
  perLayer[0].forEach((n, i) => nodeColor.set(n.id, CAT[i % CAT.length]));
  for (let li = 1; li < nLayers; li++) {
    perLayer[li].forEach((n) => nodeColor.set(n.id, colors.border4));
  }

  // Vertical layout: stack each layer's nodes, centered.
  const box = new Map<string, { y0: number; y1: number }>();
  perLayer.forEach((ns) => {
    const total = ns.reduce((s, n) => s + value(n.id) * scale, 0) + (ns.length - 1) * GAP;
    let y = PAD_Y + (height - 2 * PAD_Y - total) / 2;
    ns.forEach((n) => {
      const h = value(n.id) * scale;
      box.set(n.id, { y0: y, y1: y + h });
      y += h + GAP;
    });
  });

  const xOf = (layer: number) => {
    const i = layerIndex.get(layer)!;
    const inset = 3;
    return nLayers === 1 ? 50 : inset + (i / (nLayers - 1)) * (100 - 2 * inset);
  };

  // Running offsets so a node's links stack along its band.
  const outOff = new Map<string, number>();
  const inOff = new Map<string, number>();
  const ribbons = links
    .slice()
    .sort((a, b) => (byId.get(a.source)!.layer - byId.get(b.source)!.layer))
    .map((l, i) => {
      const s = byId.get(l.source)!;
      const t = byId.get(l.target)!;
      const sBox = box.get(l.source)!;
      const tBox = box.get(l.target)!;
      const th = l.value * scale;
      const so = outOff.get(l.source) ?? 0;
      const to = inOff.get(l.target) ?? 0;
      outOff.set(l.source, so + th);
      inOff.set(l.target, to + th);
      const sx = xOf(s.layer);
      const tx = xOf(t.layer);
      const sy0 = sBox.y0 + so;
      const sy1 = sy0 + th;
      const ty0 = tBox.y0 + to;
      const ty1 = ty0 + th;
      const mx = (sx + tx) / 2;
      // Ribbon: top edge cubic, down target side, bottom edge cubic back.
      const d = `M ${sx} ${sy0} C ${mx} ${sy0}, ${mx} ${ty0}, ${tx} ${ty0} L ${tx} ${ty1} C ${mx} ${ty1}, ${mx} ${sy1}, ${sx} ${sy1} Z`;
      return {
        d,
        color: nodeColor.get(l.source)!,
        title: `${s.label} to ${t.label}: ${l.value}`,
        key: `${l.source}-${l.target}-${i}`,
      };
    });

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <div style={{ position: "relative", width: "100%", height }}>
        <svg viewBox={`0 0 100 ${height}`} preserveAspectRatio="none" width="100%" height={height} style={{ position: "absolute", inset: 0, display: "block" }}>
          {ribbons.map((r) => (
            <path key={r.key} d={r.d} fill={r.color} fillOpacity={0.26} stroke="none">
              <title>{r.title}</title>
            </path>
          ))}
        </svg>

        {/* node bars + labels */}
        {nodes.map((n) => {
          const b = box.get(n.id)!;
          const x = xOf(n.layer);
          const li = layerIndex.get(n.layer)!;
          const labelRight = li < nLayers - 1;
          return (
            <div key={n.id}>
              <div
                title={`${n.label}: ${value(n.id)}`}
                style={{
                  position: "absolute",
                  left: `${x}%`,
                  top: b.y0,
                  height: Math.max(2, b.y1 - b.y0),
                  width: NODE_W,
                  marginLeft: -NODE_W / 2,
                  borderRadius: 2,
                  background: nodeColor.get(n.id),
                  boxShadow: `0 0 0 1px ${colors.card}`,
                }}
              />
              <div
                style={{
                  position: "absolute",
                  left: `${x}%`,
                  top: (b.y0 + b.y1) / 2,
                  transform: `translateY(-50%) ${labelRight ? "" : "translateX(-100%)"}`,
                  marginLeft: labelRight ? NODE_W : -NODE_W,
                  whiteSpace: "nowrap",
                  fontSize: 10.5,
                  color: colors.textMuted,
                  fontFamily: fonts.sans,
                  pointerEvents: "none",
                }}
              >
                {n.label}
              </div>
            </div>
          );
        })}
      </div>

      {/* legend: first-layer identity */}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
        {perLayer[0].map((n) => (
          <span key={n.id} style={{ display: "inline-flex", alignItems: "center", gap: 5, fontFamily: fonts.mono, fontSize: 9.5, color: palette.faint }}>
            <span style={{ width: 8, height: 8, borderRadius: 2, background: nodeColor.get(n.id) }} />
            {n.label}
          </span>
        ))}
      </div>
    </div>
  );
}
