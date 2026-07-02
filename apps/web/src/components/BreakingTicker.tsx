import { palette, fonts } from "../theme";

// The pack-provided signal strip: news supplies BREAKING, the library
// fallback supplies NEW IN LIBRARY — the label comes from telemetry.
interface Props {
  text: string;
  label?: string;
}

export default function BreakingTicker({ text, label = "BREAKING" }: Props) {
  return (
    <div
      style={{
        height: 30,
        flex: "none",
        background: "#140812",
        borderBottom: "1px solid #3d1430",
        display: "flex",
        alignItems: "center",
        overflow: "hidden",
      }}
    >
      <div
        style={{
          flex: "none",
          background: palette.neg,
          color: "#0a0410",
          fontFamily: fonts.mono,
          fontWeight: 600,
          fontSize: 10,
          letterSpacing: "0.14em",
          padding: "0 12px",
          height: "100%",
          display: "flex",
          alignItems: "center",
          zIndex: 2,
        }}
      >
        {label}
      </div>
      <div style={{ flex: 1, overflow: "hidden", whiteSpace: "nowrap" }}>
        <div
          style={{
            display: "inline-block",
            whiteSpace: "nowrap",
            animation: "ticker 48s linear infinite",
            fontFamily: fonts.mono,
            fontSize: 11.5,
            color: "#c7b4b6",
          }}
        >
          {text}
          {text}
        </div>
      </div>
    </div>
  );
}
