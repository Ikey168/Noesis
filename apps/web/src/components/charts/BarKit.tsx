// BarKit — one accessible horizontal-bar primitive with two modes.
//
// diverging: signed values around a central zero baseline; positive extends
//   right, negative left. Polarity is never color-alone: it is carried by side
//   (which way the bar points), the signed value label, and color together.
// grouped: several series per row, each a thin bar in a fixed-order categorical
//   color, with a legend so identity is never color-alone.
//
// Bars are HTML so ends stay crisply rounded (4px) and text stays sharp; each
// bar carries a native hover tooltip.

import { ACCENT, palette, colors, fonts } from "../../theme";

const CAT = [ACCENT, palette.violet, palette.amber, palette.blue, palette.teal];
const LABEL_W = 104;
const ROW_GAP = 8;

export interface DivergingRow {
  label: string;
  value: number;
}
export interface GroupedRow {
  label: string;
  values: { key: string; value: number }[];
}

function signed(v: number, unit: string): string {
  return `${v >= 0 ? "+" : ""}${v.toFixed(2)}${unit}`;
}

export function DivergingBars({
  rows,
  unit = "",
}: {
  rows: DivergingRow[];
  unit?: string;
}) {
  if (!rows.length) return null;
  const maxAbs = Math.max(0.01, ...rows.map((r) => Math.abs(r.value)));
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: ROW_GAP }}>
      {rows.map((r) => {
        const pos = r.value >= 0;
        const col = pos ? palette.pos : palette.neg;
        const pct = (Math.abs(r.value) / maxAbs) * 50;
        return (
          <div key={r.label} style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <div style={{ width: LABEL_W, flex: "none", fontSize: 12, fontWeight: 500, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
              {r.label}
            </div>
            <div style={{ position: "relative", flex: 1, height: 14 }}>
              {/* zero baseline */}
              <div style={{ position: "absolute", left: "50%", top: 0, bottom: 0, width: 1, background: colors.border3 }} />
              <div
                title={`${r.label}: ${signed(r.value, unit)}`}
                style={{
                  position: "absolute",
                  top: 3,
                  height: 8,
                  width: `${pct}%`,
                  background: col,
                  left: pos ? "50%" : `${50 - pct}%`,
                  borderRadius: pos ? "0 4px 4px 0" : "4px 0 0 4px",
                }}
              />
              <div
                style={{
                  position: "absolute",
                  top: -1,
                  [pos ? "left" : "right"]: pos ? `calc(50% + ${pct}% + 4px)` : `calc(50% + ${pct}% + 4px)`,
                  fontFamily: fonts.mono,
                  fontSize: 10.5,
                  color: col,
                }}
              >
                {signed(r.value, unit)}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function GroupedBars({
  rows,
  series,
  unit = "",
}: {
  rows: GroupedRow[];
  series: string[];
  unit?: string;
}) {
  if (!rows.length) return null;
  const max = Math.max(0.01, ...rows.flatMap((r) => r.values.map((v) => v.value)));
  const colorOf = (key: string) => CAT[Math.max(0, series.indexOf(key)) % CAT.length];
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <div style={{ display: "flex", flexDirection: "column", gap: ROW_GAP }}>
        {rows.map((r) => (
          <div key={r.label} style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <div style={{ width: LABEL_W, flex: "none", fontSize: 12, fontWeight: 500, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
              {r.label}
            </div>
            <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 2 }}>
              {r.values.map((v) => (
                <div
                  key={v.key}
                  title={`${r.label} · ${v.key}: ${v.value}${unit}`}
                  style={{ height: 6, width: `${Math.max(1.5, (v.value / max) * 100)}%`, background: colorOf(v.key), borderRadius: "0 4px 4px 0" }}
                />
              ))}
            </div>
          </div>
        ))}
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 10, marginTop: 2 }}>
        {series.map((s) => (
          <span key={s} style={{ display: "inline-flex", alignItems: "center", gap: 5, fontFamily: fonts.mono, fontSize: 9.5, color: palette.faint }}>
            <span style={{ width: 8, height: 8, borderRadius: 2, background: colorOf(s) }} />
            {s}
          </span>
        ))}
      </div>
    </div>
  );
}
