// LineBand — a single time series drawn as a 2px line over a shaded
// confidence band (lo..hi). One series, so no legend: the panel title names
// it. Polarity is never color-alone — a neutral-gray zero baseline anchors the
// sign and the latest value is direct-labeled and tinted by sign. The band is
// always shown, so the series is never a bare point estimate.
//
// Marks are drawn in a 0..100 SVG box with preserveAspectRatio="none" so the
// plot fills the panel width; the line uses a non-scaling stroke and the dots,
// value label and date ticks are HTML overlays so text and markers stay crisp
// and round regardless of the horizontal stretch.

import { ACCENT, palette, fonts } from "../../theme";

export interface LinePoint {
  label: string; // x-axis label (e.g. a date)
  value: number;
  lo?: number;
  hi?: number;
}

interface Props {
  points: LinePoint[];
  height?: number;
  unit?: string;
  // Diverging series (default): domain includes zero, a zero baseline is drawn,
  // and the latest value is signed and sign-tinted. Set false for a rate or
  // volume: domain follows the data, the baseline shows only if the data spans
  // zero, and the latest value is unsigned in the accent color.
  signed?: boolean;
}

const INSET_X = 2; // % horizontal padding so end dots are not clipped
const PAD_Y = 8; // % vertical padding around the value range

export default function LineBand({ points, height = 118, unit = "", signed = true }: Props) {
  if (points.length < 2) {
    return (
      <div
        style={{
          height,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          color: palette.faint,
          fontFamily: fonts.mono,
          fontSize: 11.5,
        }}
      >
        not enough points to plot a trajectory
      </div>
    );
  }

  const values = points.map((p) => p.value);
  const los = points.map((p) => (p.lo ?? p.value));
  const his = points.map((p) => (p.hi ?? p.value));
  // Diverging series pin zero into the domain so the baseline is meaningful;
  // a rate/volume series lets the domain follow the data.
  const lo = Math.min(...(signed ? [0] : []), ...los, ...values);
  const hi = Math.max(...(signed ? [0] : []), ...his, ...values);
  const span = hi - lo || 1;

  const x = (i: number) => INSET_X + (i / (points.length - 1)) * (100 - 2 * INSET_X);
  const y = (v: number) => PAD_Y + (1 - (v - lo) / span) * (100 - 2 * PAD_Y);

  const linePts = points.map((p, i) => `${x(i)},${y(p.value)}`).join(" ");
  // Band polygon: hi across, then lo back.
  const bandPts =
    points.map((p, i) => `${x(i)},${y(p.hi ?? p.value)}`).join(" ") +
    " " +
    points
      .slice()
      .reverse()
      .map((p, ri) => {
        const i = points.length - 1 - ri;
        return `${x(i)},${y(p.lo ?? p.value)}`;
      })
      .join(" ");

  const zeroY = y(0);
  const showZero = lo <= 0 && hi >= 0;
  const last = points[points.length - 1];
  const lastSign = !signed ? ACCENT : last.value >= 0 ? palette.pos : palette.neg;
  const hasBand = points.some((p) => p.lo != null && p.hi != null);
  const fmt = (v: number) => `${signed && v >= 0 ? "+" : ""}${v.toFixed(2)}${unit}`;

  // Which x labels to show: first, last, and a couple in between.
  const tickEvery = Math.max(1, Math.round((points.length - 1) / 3));

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <div style={{ position: "relative", width: "100%", height }}>
        <svg
          viewBox="0 0 100 100"
          preserveAspectRatio="none"
          width="100%"
          height={height}
          style={{ display: "block", overflow: "visible" }}
        >
          {hasBand ? <polygon points={bandPts} fill={`${ACCENT}22`} stroke="none" /> : null}
          {/* zero baseline — neutral, recessive; only when the domain spans zero */}
          {showZero ? (
            <line
              x1="0"
              x2="100"
              y1={zeroY}
              y2={zeroY}
              stroke={palette.neu}
              strokeWidth={1}
              strokeDasharray="3 3"
              vectorEffect="non-scaling-stroke"
              opacity={0.5}
            />
          ) : null}
          <polyline
            points={linePts}
            fill="none"
            stroke={ACCENT}
            strokeWidth={2}
            strokeLinejoin="round"
            strokeLinecap="round"
            vectorEffect="non-scaling-stroke"
          />
        </svg>

        {/* dot overlay — round + crisp, native hover tooltip per point */}
        {points.map((p, i) => (
          <div
            key={i}
            title={`${p.label} · ${fmt(p.value)}${
              p.lo != null && p.hi != null ? ` [${p.lo.toFixed(2)}, ${p.hi.toFixed(2)}]` : ""
            }`}
            style={{
              position: "absolute",
              left: `${x(i)}%`,
              top: `${y(p.value)}%`,
              width: i === points.length - 1 ? 8 : 5,
              height: i === points.length - 1 ? 8 : 5,
              marginLeft: i === points.length - 1 ? -4 : -2.5,
              marginTop: i === points.length - 1 ? -4 : -2.5,
              borderRadius: "50%",
              background: i === points.length - 1 ? lastSign : ACCENT,
              boxShadow: `0 0 0 2px ${"#0B151E"}`,
              cursor: "default",
            }}
          />
        ))}

        {/* latest value, direct-labeled and tinted by sign */}
        <div
          style={{
            position: "absolute",
            right: 0,
            top: `${Math.min(84, Math.max(0, y(last.value) - 16))}%`,
            fontFamily: fonts.mono,
            fontSize: 11,
            fontWeight: 600,
            color: lastSign,
            background: "#0B151Ecc",
            padding: "0 3px",
            borderRadius: 3,
          }}
        >
          {fmt(last.value)}
        </div>
      </div>

      {/* x-axis ticks */}
      <div style={{ position: "relative", height: 12 }}>
        {points.map((p, i) =>
          i % tickEvery === 0 || i === points.length - 1 ? (
            <div
              key={i}
              style={{
                position: "absolute",
                left: `${x(i)}%`,
                transform: "translateX(-50%)",
                fontFamily: fonts.mono,
                fontSize: 9,
                color: palette.faint,
                whiteSpace: "nowrap",
              }}
            >
              {p.label}
            </div>
          ) : null,
        )}
      </div>
    </div>
  );
}
