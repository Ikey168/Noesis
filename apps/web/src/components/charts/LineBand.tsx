// LineBand — a single time series drawn as a line over a shaded confidence
// band (lo..hi), built on Recharts. One series, so no legend: the panel title
// names it. Polarity is never color-alone: a neutral zero baseline anchors the
// sign for a diverging series, and the latest value is direct-labeled and
// tinted by sign. The band is shown whenever present, so the series is never a
// bare point estimate. Hovering any point reveals its value and interval.

import {
  ComposedChart,
  Area,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ReferenceLine,
  ReferenceDot,
  ResponsiveContainer,
} from "recharts";
import { ACCENT, colors, palette, fonts } from "../../theme";

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

interface Row {
  label: string;
  value: number;
  band?: [number, number];
}

function signedFmt(signed: boolean, unit: string) {
  return (v: number) => `${signed && v >= 0 ? "+" : ""}${v.toFixed(2)}${unit}`;
}

function BandTooltip(props: {
  active?: boolean;
  payload?: Array<{ payload: Row }>;
  label?: string;
  signed: boolean;
  unit: string;
}) {
  const { active, payload, label, signed, unit } = props;
  if (!active || !payload || !payload.length) return null;
  const row = payload[0].payload;
  const fmt = signedFmt(signed, unit);
  return (
    <div
      style={{
        background: "#0B151Ef2",
        border: `1px solid ${colors.border2}`,
        borderRadius: 4,
        padding: "5px 8px",
        fontFamily: fonts.mono,
        fontSize: 10.5,
        color: colors.text,
      }}
    >
      <div style={{ color: palette.faint, marginBottom: 2 }}>{label}</div>
      <div style={{ color: ACCENT, fontWeight: 600 }}>{fmt(row.value)}</div>
      {row.band ? (
        <div style={{ color: palette.neu }}>
          [{row.band[0].toFixed(2)}, {row.band[1].toFixed(2)}]
        </div>
      ) : null}
    </div>
  );
}

export default function LineBand({ points, height = 128, unit = "", signed = true }: Props) {
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

  const hasBand = points.some((p) => p.lo != null && p.hi != null);
  const data: Row[] = points.map((p) => ({
    label: p.label,
    value: p.value,
    band: p.lo != null && p.hi != null ? [p.lo, p.hi] : undefined,
  }));

  const values = points.map((p) => p.value);
  const los = points.map((p) => p.lo ?? p.value);
  const his = points.map((p) => p.hi ?? p.value);
  // Diverging series pin zero into the domain so the baseline is meaningful;
  // a rate/volume series lets the domain follow the data.
  const lo = Math.min(...(signed ? [0] : []), ...los, ...values);
  const hi = Math.max(...(signed ? [0] : []), ...his, ...values);
  const pad = (hi - lo || 1) * 0.08;

  const last = points[points.length - 1];
  const lastSign = !signed ? ACCENT : last.value >= 0 ? palette.pos : palette.neg;
  const showZero = lo <= 0 && hi >= 0;
  const fmt = signedFmt(signed, unit);

  return (
    <ResponsiveContainer width="100%" height={height}>
      <ComposedChart data={data} margin={{ top: 10, right: 44, bottom: 2, left: 4 }}>
        <XAxis
          dataKey="label"
          tickLine={false}
          axisLine={false}
          interval="preserveStartEnd"
          minTickGap={44}
          padding={{ left: 14, right: 6 }}
          tick={{ fill: palette.faint, fontFamily: fonts.mono, fontSize: 9 }}
        />
        <YAxis hide domain={[lo - pad, hi + pad]} />
        {showZero ? (
          <ReferenceLine y={0} stroke={palette.neu} strokeDasharray="3 3" strokeOpacity={0.5} />
        ) : null}
        {hasBand ? (
          <Area
            type="monotone"
            dataKey="band"
            stroke="none"
            fill={ACCENT}
            fillOpacity={0.13}
            connectNulls
            isAnimationActive={false}
            activeDot={false}
          />
        ) : null}
        <Line
          type="monotone"
          dataKey="value"
          stroke={ACCENT}
          strokeWidth={2}
          dot={{ r: 2.5, fill: ACCENT, strokeWidth: 0 }}
          activeDot={{ r: 4, fill: ACCENT, stroke: colors.card, strokeWidth: 2 }}
          isAnimationActive={false}
        />
        {/* latest value, direct-labeled and tinted by sign */}
        <ReferenceDot
          x={last.label}
          y={last.value}
          r={4}
          fill={lastSign}
          stroke={colors.card}
          strokeWidth={2}
          label={{
            value: fmt(last.value),
            position: "right",
            fill: lastSign,
            fontFamily: fonts.mono,
            fontSize: 11,
            fontWeight: 600,
          }}
        />
        <Tooltip
          cursor={{ stroke: palette.neu, strokeOpacity: 0.35 }}
          content={<BandTooltip signed={signed} unit={unit} />}
        />
      </ComposedChart>
    </ResponsiveContainer>
  );
}
