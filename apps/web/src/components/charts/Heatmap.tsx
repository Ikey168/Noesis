import { fonts } from "../../theme";
import type { Heatmap as HeatmapData } from "../../types";

// Sentiment heatmap (categories × day columns).
function cellColor(v: number): string {
  if (v > 0.05) {
    const t = Math.min(v / 0.7, 1);
    return `rgba(0,255,163,${0.18 + 0.7 * t})`;
  }
  if (v < -0.05) {
    const t = Math.min(-v / 0.7, 1);
    return `rgba(255,46,108,${0.18 + 0.7 * t})`;
  }
  return "rgba(140,165,175,0.14)";
}

interface Props {
  data: HeatmapData;
}

export default function Heatmap({ data }: Props) {
  const { topics, cols, seed, labels } = data;
  const labelEvery = cols > 10 ? 2 : 1;

  if (!topics.length) {
    return (
      <div style={{ height: 120, display: "flex", alignItems: "center", justifyContent: "center", color: "#5f7580", fontFamily: fonts.mono, fontSize: 12 }}>
        No sentiment data in the selected window
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
      {topics.map((t, ri) => (
        <div key={ri} style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <div style={{ width: 78, flex: "none", fontSize: 11.5, color: "#9ab3bb", fontFamily: fonts.sans }}>
            {t}
          </div>
          <div style={{ flex: 1, display: "grid", gridTemplateColumns: `repeat(${cols},1fr)`, gap: 4 }}>
            {seed[ri].map((v, ci) => (
              <div
                key={ci}
                title={`${t} · ${labels[ci] ?? ""} · ${v.toFixed(2)}`}
                style={{ height: 26, borderRadius: 3, background: cellColor(v) }}
              />
            ))}
          </div>
        </div>
      ))}
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 2 }}>
        <div style={{ width: 78, flex: "none" }} />
        <div style={{ flex: 1, display: "grid", gridTemplateColumns: `repeat(${cols},1fr)`, gap: 4 }}>
          {labels.map((h, i) => (
            <div key={i} style={{ textAlign: "center", fontFamily: fonts.mono, fontSize: 9, color: "#4b6470" }}>
              {i % labelEvery === 0 ? h : ""}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
