// TimelineAxis — events laid on a true dated horizontal axis. Each event sits
// at its timestamp (uneven spacing is the point), its marker sized by coverage
// volume and colored by sentiment. Sentiment is never color-alone: the stalk is
// solid for positive and dashed for negative, and each label carries the signed
// value. Labels alternate above/below the axis with a connecting stalk so they
// do not collide.

import { colors, palette, fonts } from "../../theme";

export interface TimelineEvent {
  t: number; // position on the axis (e.g. day offset); larger = later
  date: string; // display label for the timestamp
  label: string; // what happened
  volume: number; // magnitude -> marker radius
  sentiment: number; // -1..1 -> marker color + stalk style (a signed signal)
  tag?: string; // optional short caption shown instead of the signed value
}

const DEFAULT_LEGEND =
  "marker size = coverage volume · color = sentiment (green up / red down) · dashed stalk = negative";
const INSET = 7; // % horizontal padding so end markers are not clipped

function sentColor(v: number): string {
  if (v > 0.05) return palette.pos;
  if (v < -0.05) return palette.neg;
  return palette.neu;
}

function signed(v: number): string {
  return `${v >= 0 ? "+" : ""}${v.toFixed(2)}`;
}

export default function TimelineAxis({
  events,
  height = 150,
  legend = DEFAULT_LEGEND,
}: {
  events: TimelineEvent[];
  height?: number;
  legend?: string;
}) {
  if (!events.length) {
    return (
      <div style={{ height, display: "flex", alignItems: "center", justifyContent: "center", color: palette.faint, fontFamily: fonts.mono, fontSize: 11.5 }}>
        no dated events in the window
      </div>
    );
  }

  const sorted = [...events].sort((a, b) => a.t - b.t);
  const ts = sorted.map((e) => e.t);
  const tMin = Math.min(...ts);
  const tMax = Math.max(...ts);
  const span = tMax - tMin || 1;
  const x = (t: number) => INSET + ((t - tMin) / span) * (100 - 2 * INSET);
  const vMax = Math.max(1, ...sorted.map((e) => e.volume));
  const r = (v: number) => 4 + (v / vMax) * 6; // 4..10 px radius

  const axisY = height / 2;
  const labelTop = 6;
  const labelBottom = height - 34;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <div style={{ position: "relative", width: "100%", height }}>
        {/* axis */}
        <div style={{ position: "absolute", left: 0, right: 0, top: axisY, height: 1, background: colors.border3 }} />

        {sorted.map((e, i) => {
          const above = i % 2 === 0;
          const cx = x(e.t);
          const rad = r(e.volume);
          const col = sentColor(e.sentiment);
          const labelY = above ? labelTop : labelBottom;
          const stalkTop = above ? labelTop + 24 : axisY;
          const stalkH = above ? axisY - (labelTop + 24) : labelBottom - axisY;
          return (
            <div key={i}>
              {/* stalk: solid = positive sentiment, dashed = negative */}
              <div
                style={{
                  position: "absolute",
                  left: `${cx}%`,
                  top: stalkTop,
                  height: Math.max(0, stalkH),
                  width: 0,
                  borderLeft: `1px ${e.sentiment < -0.05 ? "dashed" : "solid"} ${col}66`,
                }}
              />
              {/* marker */}
              <div
                title={`${e.date} · ${e.label} · vol ${e.volume} · ${e.tag ?? signed(e.sentiment)}`}
                style={{
                  position: "absolute",
                  left: `${cx}%`,
                  top: axisY,
                  width: rad * 2,
                  height: rad * 2,
                  marginLeft: -rad,
                  marginTop: -rad,
                  borderRadius: "50%",
                  background: col,
                  boxShadow: `0 0 0 2px ${colors.card}`,
                  cursor: "default",
                }}
              />
              {/* label group */}
              <div
                style={{
                  position: "absolute",
                  left: `${cx}%`,
                  top: labelY,
                  transform: "translateX(-50%)",
                  width: 96,
                  textAlign: "center",
                  pointerEvents: "none",
                }}
              >
                <div style={{ fontSize: 11, fontWeight: 500, color: colors.text, lineHeight: 1.2, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {e.label}
                </div>
                <div style={{ fontFamily: fonts.mono, fontSize: 9, color: palette.faint }}>
                  {e.date} · <span style={{ color: col }}>{e.tag ?? signed(e.sentiment)}</span>
                </div>
              </div>
            </div>
          );
        })}
      </div>
      <div style={{ fontFamily: fonts.mono, fontSize: 10, color: palette.faint }}>{legend}</div>
    </div>
  );
}
