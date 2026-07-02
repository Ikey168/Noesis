// Design tokens for the neon-terminal theme. Kept in sync with the CSS
// variables in index.css (which drive the Tailwind/shadcn layer); these
// constants feed the inline-styled charts and panel content.

export const ACCENT = "#00E5FF";

export const palette = {
  pos: "#00FFA3",
  neg: "#FF2E6C",
  neu: "#8CA5AF",
  teal: "#00D4B8",
  amber: "#FFE347",
  blue: "#4DA8FF",
  violet: "#B366FF",
  dim: "#8CA5AF",
  faint: "#5F7580",
} as const;

export const colors = {
  bg: "#060A0F",
  sidebar: "#070D13",
  card: "#0B151E",
  cardInner: "#0A121A",
  input: "#0A141D",
  text: "#E4F4F7",
  textMuted: "#C2D6DB",
  textSubtle: "#9AB3BB",
  borderSoft: "#12242E",
  border: "#193039",
  border2: "#1F3B47",
  border3: "#26485A",
  border4: "#33607A",
  faint2: "#4B6470",
} as const;

export const fonts = {
  grotesk: "'Space Grotesk', system-ui, sans-serif",
  mono: "'IBM Plex Mono', monospace",
  sans: "'IBM Plex Sans', system-ui, sans-serif",
} as const;

// Hex-with-alpha helpers, matching the design's `acc + '1a'` style suffixes.
export const accentSoft = (accent: string) => `${accent}1a`;
export const accentBorder = (accent: string) => `${accent}55`;
export const accentGlow = (accent: string) => `${accent}66`;
export const tint = (hex: string, suffix: string) => `${hex}${suffix}`;
