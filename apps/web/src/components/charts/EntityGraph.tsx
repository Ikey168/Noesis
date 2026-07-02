import { useMemo } from "react";
import { ACCENT, fonts } from "../../theme";
import type { LiveGraph } from "../../types";

interface Props {
  data: LiveGraph;
  accent?: string;
}

interface Pos {
  x: number;
  y: number;
}

// Deterministic force-directed layout (small graphs, so O(n^2) is fine).
function layout(data: LiveGraph): Record<string, Pos> {
  const nodes = data.nodes;
  const n = nodes.length;
  const pos: Record<string, Pos> = {};
  if (!n) return pos;

  // Seed on a circle for determinism.
  nodes.forEach((node, i) => {
    const a = (2 * Math.PI * i) / n;
    pos[node.id] = { x: 0.5 + 0.35 * Math.cos(a), y: 0.5 + 0.35 * Math.sin(a) };
  });

  const adj = data.edges;
  const iterations = 300;
  const repulsion = 0.018;
  const spring = 0.02;
  const gravity = 0.008;

  for (let it = 0; it < iterations; it++) {
    const disp: Record<string, Pos> = {};
    nodes.forEach((nd) => (disp[nd.id] = { x: 0, y: 0 }));

    // Repulsion between every pair.
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        const a = nodes[i].id;
        const b = nodes[j].id;
        let dx = pos[a].x - pos[b].x;
        let dy = pos[a].y - pos[b].y;
        let d2 = dx * dx + dy * dy || 0.0001;
        const f = repulsion / d2;
        const d = Math.sqrt(d2);
        dx /= d;
        dy /= d;
        disp[a].x += dx * f;
        disp[a].y += dy * f;
        disp[b].x -= dx * f;
        disp[b].y -= dy * f;
      }
    }

    // Spring attraction along edges (stronger for heavier edges).
    for (const [s, t, w] of adj) {
      if (!pos[s] || !pos[t]) continue;
      const dx = pos[t].x - pos[s].x;
      const dy = pos[t].y - pos[s].y;
      const f = spring * Math.min(3, w);
      disp[s].x += dx * f;
      disp[s].y += dy * f;
      disp[t].x -= dx * f;
      disp[t].y -= dy * f;
    }

    // Gravity toward the center + integrate.
    nodes.forEach((nd) => {
      disp[nd.id].x += (0.5 - pos[nd.id].x) * gravity;
      disp[nd.id].y += (0.5 - pos[nd.id].y) * gravity;
      pos[nd.id].x += Math.max(-0.04, Math.min(0.04, disp[nd.id].x));
      pos[nd.id].y += Math.max(-0.04, Math.min(0.04, disp[nd.id].y));
    });
  }

  // Normalize into a padded box.
  const xs = nodes.map((nd) => pos[nd.id].x);
  const ys = nodes.map((nd) => pos[nd.id].y);
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  const minY = Math.min(...ys), maxY = Math.max(...ys);
  const spanX = maxX - minX || 1;
  const spanY = maxY - minY || 1;
  nodes.forEach((nd) => {
    pos[nd.id].x = 0.08 + 0.84 * ((pos[nd.id].x - minX) / spanX);
    pos[nd.id].y = 0.12 + 0.78 * ((pos[nd.id].y - minY) / spanY);
  });
  return pos;
}

export default function EntityGraph({ data, accent = ACCENT }: Props) {
  const W = 760;
  const H = 480;
  const pos = useMemo(() => layout(data), [data]);

  const maxCount = Math.max(1, ...data.nodes.map((n) => n.count));
  const radius = (count: number) => 11 + 18 * Math.sqrt(count / maxCount);
  const maxW = Math.max(1, ...data.edges.map((e) => e[2]));
  const px = (id: string) => (pos[id]?.x ?? 0.5) * W;
  const py = (id: string) => (pos[id]?.y ?? 0.5) * H;

  if (!data.nodes.length) {
    return (
      <div style={{ height: H, display: "flex", alignItems: "center", justifyContent: "center", color: "#5f7580", fontFamily: fonts.mono, fontSize: 12 }}>
        No entities in the selected window
      </div>
    );
  }

  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} style={{ display: "block" }}>
      <g>
        {data.edges.map((e, i) => (
          <line
            key={i}
            x1={px(e[0])}
            y1={py(e[0])}
            x2={px(e[1])}
            y2={py(e[1])}
            stroke="#26485a"
            strokeWidth={0.8 + 1.6 * (e[2] / maxW)}
            opacity={0.5 + 0.5 * (e[2] / maxW)}
          />
        ))}
      </g>
      <g>
        {data.nodes.map((n, i) => {
          const r = radius(n.count);
          const color = n.color || accent;
          return (
            <g key={i}>
              <circle cx={px(n.id)} cy={py(n.id)} r={r + 5} fill={color} opacity={0.1} />
              <circle cx={px(n.id)} cy={py(n.id)} r={r} fill="#0a121a" stroke={color} strokeWidth={2} />
              <circle cx={px(n.id)} cy={py(n.id)} r={Math.max(3, r * 0.28)} fill={color} />
              <text
                x={px(n.id)}
                y={py(n.id) + r + 13}
                textAnchor="middle"
                fill="#c2d6db"
                fontSize={11}
                fontFamily={fonts.sans}
                fontWeight={500}
              >
                {n.label}
              </text>
            </g>
          );
        })}
      </g>
    </svg>
  );
}
