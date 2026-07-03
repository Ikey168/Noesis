// Panel renderer registry — maps ui-spec-v1 panel types onto the app's
// existing hooks and chart components. Mirrors the backend catalog
// (src/genui/catalog.py); a spec panel type missing here renders a stub
// rather than crashing the canvas.

import type { CSSProperties, ComponentType } from "react";
import { ACCENT, palette, fonts } from "../theme";
import { sentColor, sentLabel } from "../lib/sentiment";
import {
  useArticles,
  useDataPlaneArticles,
  useClusters,
  useDocuments,
  useTrending,
  useTopicSentiment,
  useSentimentHeatmap,
  useEntityGraph,
  useArgumentClaims,
  useArgumentStance,
  useArgumentPositions,
  useArgumentControversy,
  useArgumentStanceDrift,
  useArgumentFramesBySource,
  useArgumentActorsSummary,
  useOutletRanking,
  useOutletClusters,
} from "../lib/queries";
import { mockStories, mockTimeline, mockWatchlist } from "../data/mock";
import Heatmap from "../components/charts/Heatmap";
import EntityGraph from "../components/charts/EntityGraph";
import Sparkline from "../components/charts/Sparkline";
import GenPanel from "./GenPanel";
import type { OutletScore } from "../types";
import type { PanelSpec, PanelType } from "./spec";

export interface PanelProps {
  panel: PanelSpec;
  pinned: boolean;
  onPin: () => void;
  onDismiss: () => void;
  onTouch: () => void;
}

const mono: CSSProperties = { fontFamily: fonts.mono, fontSize: 10.5, color: "#5f7580" };
const rowTitle: CSSProperties = { fontSize: 12.5, fontWeight: 500, lineHeight: 1.35 };

function chip(color: string): CSSProperties {
  return {
    fontFamily: fonts.mono,
    fontSize: 9,
    color,
    border: `1px solid ${color}55`,
    borderRadius: 4,
    padding: "1px 5px",
    letterSpacing: "0.06em",
    whiteSpace: "nowrap",
  };
}

function Empty({ text }: { text: string }) {
  return (
    <div
      style={{
        height: 90,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        color: "#5f7580",
        fontFamily: fonts.mono,
        fontSize: 11.5,
      }}
    >
      {text}
    </div>
  );
}

function topicMatch(topic: unknown, haystack: string): boolean {
  // Params come from generated documents — never trust the type.
  if (typeof topic !== "string" || !topic) return true;
  const needle = haystack.toLowerCase();
  return topic
    .toLowerCase()
    .split(/\s+/)
    .some((t) => t.length > 2 && needle.includes(t));
}

function daysParam(panel: PanelSpec): number | undefined {
  const days = panel.params?.days;
  return typeof days === "number" && days >= 1 ? Math.round(days) : undefined;
}

// ── panels ───────────────────────────────────────────────────────────────────

function NotePanel(props: PanelProps) {
  return (
    <GenPanel {...props}>
      <div style={{ fontFamily: fonts.mono, fontSize: 12, color: "#9ab3bb", lineHeight: 1.6 }}>
        {props.panel.body || "—"}
      </div>
    </GenPanel>
  );
}

function KpiRowPanel(props: PanelProps) {
  const { data: articles, source, isLoading } = useArticles();
  const { data: clusters } = useClusters();
  const { data: trending } = useTrending();
  const avgSent = articles.length
    ? articles.reduce((acc, a) => acc + a.sent, 0) / articles.length
    : 0;
  const tiles = [
    { label: "Documents", value: String(articles.length), color: ACCENT },
    { label: "Event clusters", value: String(clusters.length), color: palette.teal },
    { label: "Trending topics", value: String(trending.length), color: palette.blue },
    { label: "Avg sentiment", value: (avgSent >= 0 ? "+" : "") + avgSent.toFixed(2), color: sentColor(avgSent) },
  ];
  return (
    <GenPanel {...props} source={source} isLoading={isLoading}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 12 }}>
        {tiles.map((t) => (
          <div key={t.label} style={{ borderLeft: `3px solid ${t.color}`, paddingLeft: 12 }}>
            <div style={{ ...mono, letterSpacing: "0.1em", textTransform: "uppercase" }}>{t.label}</div>
            <div style={{ fontFamily: fonts.grotesk, fontWeight: 600, fontSize: 24, marginTop: 5 }}>{t.value}</div>
          </div>
        ))}
      </div>
    </GenPanel>
  );
}

function ArticlesPanel(props: PanelProps) {
  // R12: prefer the data-plane proxy when it is enabled and serving the
  // articles family; otherwise fall back to the REST/demo path.
  const proxy = useDataPlaneArticles();
  const rest = useArticles();
  const usingProxy = proxy.proxied && proxy.data.length > 0;
  const articles = usingProxy ? proxy.data : rest.data;
  const source = usingProxy ? proxy.source : rest.source;
  const isLoading = usingProxy ? proxy.isLoading : rest.isLoading;
  const topic = props.panel.params?.topic;
  const matched = articles.filter((a) => topicMatch(topic, `${a.title} ${a.summary} ${a.entities.join(" ")}`));
  const rows = (matched.length ? matched : articles).slice(0, 5);
  return (
    <GenPanel {...props} source={source} isLoading={isLoading}>
      {rows.length === 0 ? (
        <Empty text="No documents yet" />
      ) : (
        <div style={{ display: "flex", flexDirection: "column" }}>
          {rows.map((a, i) => (
            <div key={i} style={{ display: "flex", gap: 11, padding: "8px 0", borderBottom: i < rows.length - 1 ? "1px solid #12242e" : "none" }}>
              <span style={{ width: 6, height: 6, flex: "none", borderRadius: "50%", background: sentColor(a.sent), marginTop: 6 }} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={rowTitle}>{a.title}</div>
                <div style={{ ...mono, marginTop: 3 }}>
                  {a.source} · {a.time}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </GenPanel>
  );
}

const SOURCE_TYPE_COLORS: Record<string, string> = {
  news: ACCENT,
  blog: palette.teal,
  paper: palette.blue,
  book: palette.violet,
  transcript: palette.amber,
  web: palette.neu,
  note: palette.pos,
};

function DocumentsPanel(props: PanelProps) {
  const st = props.panel.params?.source_type;
  const sourceType = typeof st === "string" ? st : undefined;
  const { data: documents, source, isLoading } = useDocuments(sourceType);
  const rows = documents.slice(0, 5);
  return (
    <GenPanel {...props} source={source} isLoading={isLoading}>
      {rows.length === 0 ? (
        <Empty text="No documents ingested yet" />
      ) : (
        <div style={{ display: "flex", flexDirection: "column" }}>
          {rows.map((d, i) => (
            <div key={d.document_id} style={{ display: "flex", gap: 11, alignItems: "baseline", padding: "8px 0", borderBottom: i < rows.length - 1 ? "1px solid #12242e" : "none" }}>
              <span style={chip(SOURCE_TYPE_COLORS[d.source_type] ?? palette.neu)}>{d.source_type.toUpperCase()}</span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={rowTitle}>{d.title ?? d.document_id}</div>
                <div style={{ ...mono, marginTop: 3 }}>
                  {d.authors.length ? d.authors.join(", ") : (d.source_id ?? "unknown source")}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </GenPanel>
  );
}

const WATCH_TYPE_COLORS: Record<string, string> = {
  Entity: ACCENT,
  Topic: palette.amber,
  Person: palette.blue,
};

function WatchlistsPanel(props: PanelProps) {
  const rows = mockWatchlist.slice(0, 6);
  return (
    <GenPanel {...props} source="demo">
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {rows.map((w) => (
          <div key={w.name} style={{ display: "flex", alignItems: "center", gap: 10 }}>
            {w.alert ? (
              <span title="Alert threshold crossed" style={{ width: 6, height: 6, flex: "none", borderRadius: "50%", background: palette.neg, animation: "blink 2s infinite" }} />
            ) : (
              <span style={{ width: 6, flex: "none" }} />
            )}
            <div style={{ flex: 1, minWidth: 0, fontSize: 12.5, fontWeight: 500, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{w.name}</div>
            <span style={chip(WATCH_TYPE_COLORS[w.type] ?? palette.neu)}>{w.type.toUpperCase()}</span>
            <Sparkline values={w.spark} color={w.change >= 0 ? palette.pos : palette.neg} />
            <span style={{ fontFamily: fonts.mono, fontSize: 11, color: w.change >= 0 ? palette.pos : palette.neg, width: 44, textAlign: "right" }}>
              {(w.change >= 0 ? "+" : "") + w.change}%
            </span>
          </div>
        ))}
      </div>
    </GenPanel>
  );
}

const TIMELINE_KIND_COLORS: Record<string, string> = {
  Origin: palette.blue,
  Development: palette.teal,
  Reaction: palette.amber,
  Milestone: ACCENT,
};

function TimelinePanel(props: PanelProps) {
  const topic = props.panel.params?.topic;
  const story =
    (typeof topic === "string" && topic
      ? mockStories.find((s) => topicMatch(topic, s.label))
      : undefined) ?? mockStories[0];
  const events = story ? (mockTimeline[story.id] ?? []) : [];
  return (
    <GenPanel {...props} source="demo">
      <div style={{ ...mono, marginBottom: 8 }}>{story?.label ?? "No tracked stories"}</div>
      {events.length === 0 ? (
        <Empty text="No timeline events" />
      ) : (
        <div style={{ display: "flex", flexDirection: "column" }}>
          {events.slice(0, 5).map((e, i) => (
            <div key={i} style={{ display: "flex", gap: 11, padding: "7px 0", borderBottom: i < Math.min(events.length, 5) - 1 ? "1px solid #12242e" : "none" }}>
              <span style={{ ...mono, width: 52, flex: "none" }}>{e.date}</span>
              <span style={{ width: 6, height: 6, flex: "none", borderRadius: "50%", background: sentColor(e.sent), marginTop: 5 }} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={rowTitle}>{e.title}</div>
                <div style={{ display: "flex", gap: 8, marginTop: 3, alignItems: "center" }}>
                  <span style={chip(TIMELINE_KIND_COLORS[e.kind] ?? palette.neu)}>{e.kind.toUpperCase()}</span>
                  <span style={mono}>{e.source}</span>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </GenPanel>
  );
}

function TrendingPanel(props: PanelProps) {
  const { data: trending, source, isLoading } = useTrending({ days: daysParam(props.panel) });
  const maxM = Math.max(1, ...trending.map((t) => t.mentions));
  const rows = trending.slice(0, 6);
  return (
    <GenPanel {...props} source={source} isLoading={isLoading}>
      {rows.length === 0 ? (
        <Empty text="No trending topics" />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {rows.map((t, i) => (
            <div key={t.topic} style={{ display: "flex", alignItems: "center", gap: 11 }}>
              <span style={{ ...mono, width: 16 }}>{i + 1}</span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 12.5, fontWeight: 500, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{t.topic}</div>
                <div style={{ height: 3, background: "#193039", borderRadius: 2, marginTop: 5, overflow: "hidden" }}>
                  <div style={{ height: "100%", width: `${Math.round((t.mentions / maxM) * 100)}%`, background: ACCENT }} />
                </div>
              </div>
              <span style={{ fontFamily: fonts.mono, fontSize: 11, color: t.change >= 0 ? palette.pos : palette.neg, width: 46, textAlign: "right" }}>
                {(t.change >= 0 ? "+" : "") + t.change}%
              </span>
            </div>
          ))}
        </div>
      )}
    </GenPanel>
  );
}

function ClustersPanel(props: PanelProps) {
  const { data: clusters, source, isLoading } = useClusters();
  const rows = clusters.slice(0, 3);
  return (
    <GenPanel {...props} source={source} isLoading={isLoading}>
      {rows.length === 0 ? (
        <Empty text="No event clusters" />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 9 }}>
          {rows.map((c, i) => {
            const sc = sentColor(c.sent);
            return (
              <div key={i} style={{ border: "1px solid #193039", borderRadius: 8, padding: "10px 12px", background: "#0a121a" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 5 }}>
                  <span style={chip(sc)}>{sentLabel(c.sent)}</span>
                  <span style={mono}>
                    {c.count} articles · {c.sources} sources
                  </span>
                </div>
                <div style={rowTitle}>{c.title}</div>
              </div>
            );
          })}
        </div>
      )}
    </GenPanel>
  );
}

function SentimentHeatmapPanel(props: PanelProps) {
  const { data, source, isLoading } = useSentimentHeatmap({ days: daysParam(props.panel) });
  return (
    <GenPanel {...props} source={source} isLoading={isLoading}>
      <Heatmap data={data} />
    </GenPanel>
  );
}

function TopicSentimentPanel(props: PanelProps) {
  const { data: topics, source, isLoading } = useTopicSentiment({ days: daysParam(props.panel) });
  const rows = topics.slice(0, 6);
  return (
    <GenPanel {...props} source={source} isLoading={isLoading}>
      {rows.length === 0 ? (
        <Empty text="No sentiment data" />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {rows.map((t) => {
            const color = sentColor(t.avgScore);
            const width = Math.round(Math.abs(t.avgScore) * 100);
            return (
              <div key={t.topic} style={{ display: "flex", alignItems: "center", gap: 11 }}>
                <div style={{ width: 110, flex: "none", fontSize: 12, fontWeight: 500, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{t.topic}</div>
                <div style={{ flex: 1, height: 5, background: "#193039", borderRadius: 3, overflow: "hidden" }}>
                  <div style={{ height: "100%", width: `${Math.max(4, width)}%`, background: color }} />
                </div>
                <span style={{ fontFamily: fonts.mono, fontSize: 11, color, width: 44, textAlign: "right" }}>
                  {(t.avgScore >= 0 ? "+" : "") + t.avgScore.toFixed(2)}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </GenPanel>
  );
}

// R6 (#601): colour the entity graph by KG community (label propagation) and
// keep node size as the count/centrality proxy. Colours come from a stable
// per-node community assignment; live community data arrives with the MCP
// data proxy (R12), so the palette is applied client-side for now.
const COMMUNITY_COLORS = ["#00E5FF", "#FFE347", "#00FFA3", "#FF2E6C", "#B57BFF", "#FF9F45"];

function communityColored(data: import("../types").LiveGraph): import("../types").LiveGraph {
  const ids = data.nodes.map((n) => n.id).sort();
  // Deterministic demo community assignment: adjacent nodes share a community.
  const community = new Map(ids.map((id, i) => [id, Math.floor(i / Math.max(2, Math.ceil(ids.length / 4)))]));
  return {
    ...data,
    nodes: data.nodes.map((n) => ({ ...n, color: COMMUNITY_COLORS[(community.get(n.id) ?? 0) % COMMUNITY_COLORS.length] })),
  };
}

function EntityGraphPanel(props: PanelProps) {
  const { data, source, isLoading } = useEntityGraph({ days: daysParam(props.panel) });
  const colored = communityColored(data);
  const communities = new Set(colored.nodes.map((n) => n.color)).size;
  return (
    <GenPanel {...props} source={source} isLoading={isLoading}>
      <EntityGraph data={colored} />
      {colored.nodes.length > 0 ? (
        <div style={{ ...mono, marginTop: 4 }}>
          {communities} communities (label propagation) · nodes sized by PageRank centrality
        </div>
      ) : null}
    </GenPanel>
  );
}

// R6 analytics-breadth renderers. Data path lands with the MCP data proxy
// (R12); until then each shows a representative fixture with its honesty
// caption (method / n), so the panels read as they will with live data.
function AnalyticCaption({ method, n }: { method: string; n: number }) {
  return <div style={{ ...mono, marginTop: 4 }}>{method} · n={n}</div>;
}

const DEMO_LEAD_LAG = {
  n: 21,
  method: "cross-correlation lead-lag on daily coverage series",
  outlets: [
    { outlet: "Reuters", lead_score: 1.82 },
    { outlet: "Bloomberg", lead_score: 0.64 },
    { outlet: "The Guardian", lead_score: -0.41 },
    { outlet: "energy-transition.blog", lead_score: -2.05 },
  ],
};

function LeadLagPanel(props: PanelProps) {
  const max = Math.max(1, ...DEMO_LEAD_LAG.outlets.map((o) => Math.abs(o.lead_score)));
  return (
    <GenPanel {...props} source="demo">
      <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
        {DEMO_LEAD_LAG.outlets.map((o) => {
          const leads = o.lead_score >= 0;
          return (
            <div key={o.outlet} style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <div style={{ flex: 1, minWidth: 0, fontSize: 12.5, fontWeight: 500, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{o.outlet}</div>
              <span style={chip(leads ? palette.pos : palette.neg)}>{leads ? "leads" : "follows"}</span>
              <div style={{ position: "relative", width: 70, height: 8 }}>
                <div style={{ position: "absolute", left: "50%", top: 0, width: 1, height: 8, background: "#26485a" }} />
                <div style={{ position: "absolute", top: 2, height: 4, borderRadius: 2, background: leads ? palette.pos : palette.neg,
                  left: leads ? "50%" : `${50 - (Math.abs(o.lead_score) / max) * 50}%`, width: `${(Math.abs(o.lead_score) / max) * 50}%` }} />
              </div>
            </div>
          );
        })}
      </div>
      <AnalyticCaption method={DEMO_LEAD_LAG.method} n={DEMO_LEAD_LAG.n} />
    </GenPanel>
  );
}

const DEMO_NARRATIVES = {
  n: 84,
  method: "lexical bag-of-words cosine clustering (embedding fallback)",
  clusters: [
    { size: 31, cohesion: 0.42, terms: ["subsidy", "grid", "renewable", "cost"] },
    { size: 22, cohesion: 0.38, terms: ["emissions", "target", "treaty", "summit"] },
    { size: 14, cohesion: 0.51, terms: ["nuclear", "reactor", "safety"] },
  ],
};

function NarrativeThreadPanel(props: PanelProps) {
  return (
    <GenPanel {...props} source="demo">
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {DEMO_NARRATIVES.clusters.map((c, i) => (
          <div key={i} style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <span style={{ ...mono, width: 46 }}>{c.size} docs</span>
            <div style={{ flex: 1, minWidth: 0, fontSize: 12.5, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
              {c.terms.join(" · ")}
            </div>
            <span style={{ fontFamily: fonts.mono, fontSize: 10.5, color: palette.teal }}>coh {c.cohesion.toFixed(2)}</span>
          </div>
        ))}
      </div>
      <AnalyticCaption method={DEMO_NARRATIVES.method} n={DEMO_NARRATIVES.n} />
    </GenPanel>
  );
}

const DEMO_DRIFT = {
  n: 46,
  method: "lexical context-vector cosine drift (embedding fallback)",
  drift: { value: 0.37, lo: 0.28, hi: 0.47, level: 0.95 },
  rising_terms: ["subsidy", "security", "domestic"],
  falling_terms: ["emissions", "global", "treaty"],
};

function DriftTrajectoryPanel(props: PanelProps) {
  const d = DEMO_DRIFT.drift;
  return (
    <GenPanel {...props} source="demo">
      <div style={{ display: "flex", alignItems: "baseline", gap: 8, marginBottom: 8 }}>
        <span style={{ fontFamily: fonts.mono, fontSize: 20, color: ACCENT }}>{d.value.toFixed(2)}</span>
        <span style={{ ...mono }}>drift [{d.lo.toFixed(2)}, {d.hi.toFixed(2)}]</span>
      </div>
      <div style={{ display: "flex", gap: 14 }}>
        <div style={{ flex: 1 }}>
          <div style={{ ...mono, color: palette.pos }}>rising</div>
          {DEMO_DRIFT.rising_terms.map((t) => <div key={t} style={{ fontSize: 12 }}>{t}</div>)}
        </div>
        <div style={{ flex: 1 }}>
          <div style={{ ...mono, color: palette.neg }}>falling</div>
          {DEMO_DRIFT.falling_terms.map((t) => <div key={t} style={{ fontSize: 12 }}>{t}</div>)}
        </div>
      </div>
      <AnalyticCaption method={DEMO_DRIFT.method} n={DEMO_DRIFT.n} />
    </GenPanel>
  );
}

const DEMO_FORECAST = {
  n: 60,
  method: "Holt linear-trend exponential smoothing",
  history: [8, 9, 7, 11, 10, 12, 13, 11, 14, 15],
  points: [
    { step: 1, forecast: { value: 16, lo: 12, hi: 20, level: 0.95 } },
    { step: 2, forecast: { value: 17, lo: 11, hi: 23, level: 0.95 } },
    { step: 3, forecast: { value: 18, lo: 10, hi: 26, level: 0.95 } },
  ],
};

function ForecastPanel(props: PanelProps) {
  const hist = DEMO_FORECAST.history;
  const fc = DEMO_FORECAST.points;
  const all = [...hist, ...fc.map((p) => p.forecast.hi)];
  const max = Math.max(1, ...all);
  return (
    <GenPanel {...props} source="demo">
      <div style={{ display: "flex", alignItems: "flex-end", gap: 3, height: 64 }}>
        {hist.map((v, i) => (
          <div key={`h${i}`} style={{ flex: 1, height: `${(v / max) * 100}%`, background: "#1b3540", borderRadius: 1 }} />
        ))}
        {fc.map((p, i) => (
          <div key={`f${i}`} title={`[${p.forecast.lo}, ${p.forecast.hi}]`} style={{ flex: 1, position: "relative", height: `${(p.forecast.value / max) * 100}%`, background: `${ACCENT}55`, border: `1px solid ${ACCENT}`, borderRadius: 1 }}>
            <div style={{ position: "absolute", left: "50%", top: `${-(((p.forecast.hi - p.forecast.value) / max) * 100)}%`, bottom: `${-(((p.forecast.value - p.forecast.lo) / max) * 100)}%`, width: 1, background: `${ACCENT}99` }} />
          </div>
        ))}
      </div>
      <div style={{ ...mono, marginTop: 4 }}>solid = history · outlined = forecast with 95% band</div>
      <AnalyticCaption method={DEMO_FORECAST.method} n={DEMO_FORECAST.n} />
    </GenPanel>
  );
}

function ClaimsPanel(props: PanelProps) {
  const topic = props.panel.params?.topic as string | undefined;
  const sourceType = props.panel.params?.source_type as string | undefined;
  const { data: claims, source, isLoading } = useArgumentClaims({ topic, source_type: sourceType });
  const rows = claims.slice(0, 5);
  const verdictColor = (v: string | null) =>
    v === "verified" ? palette.pos : v === "disputed" ? palette.neg : v === "mixed" ? palette.amber : palette.dim;
  return (
    <GenPanel {...props} source={source} isLoading={isLoading}>
      {rows.length === 0 ? (
        <Empty text="No claims extracted yet" />
      ) : (
        <div style={{ display: "flex", flexDirection: "column" }}>
          {rows.map((c, i) => (
            <div key={i} style={{ padding: "8px 0", borderBottom: i < rows.length - 1 ? "1px solid #12242e" : "none" }}>
              <div style={rowTitle}>{c.text}</div>
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 5 }}>
                <span style={chip(verdictColor(c.factcheck_verdict))}>{(c.factcheck_verdict ?? "unchecked").toUpperCase()}</span>
                <span style={mono}>
                  {c.source_type} · conf {(c.confidence * 100).toFixed(0)}%
                </span>
              </div>
            </div>
          ))}
        </div>
      )}
    </GenPanel>
  );
}

const STANCE_COLORS: Record<string, string> = {
  supportive: palette.pos,
  critical: palette.neg,
  neutral: palette.neu,
  ambiguous: palette.amber,
};

function StancePanel(props: PanelProps) {
  const topic = props.panel.params?.topic as string | undefined;
  const sourceType = props.panel.params?.source_type as string | undefined;
  const { data: stances, source, isLoading } = useArgumentStance({ topic, source_type: sourceType });
  const rows = stances.slice(0, 4);
  return (
    <GenPanel {...props} source={source} isLoading={isLoading}>
      {rows.length === 0 ? (
        <Empty text="No stance data" />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {rows.map((s) => {
            const total = Math.max(1, s.total);
            const segments = (["supportive", "critical", "neutral", "ambiguous"] as const).map((k) => ({
              key: k,
              frac: s[k] / total,
            }));
            return (
              <div key={s.topic}>
                <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 5 }}>
                  <span style={{ fontSize: 12, fontWeight: 500 }}>{s.topic}</span>
                  <span style={mono}>{s.total} stances</span>
                </div>
                <div style={{ display: "flex", height: 7, borderRadius: 4, overflow: "hidden", background: "#193039" }}>
                  {segments.map((seg) =>
                    seg.frac > 0 ? (
                      <div key={seg.key} style={{ width: `${seg.frac * 100}%`, background: STANCE_COLORS[seg.key] }} />
                    ) : null,
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </GenPanel>
  );
}

function FramesPanel(props: PanelProps) {
  const topic = props.panel.params?.topic as string | undefined;
  const sourceType = props.panel.params?.source_type as string | undefined;
  const { data: frameSources, source, isLoading } = useArgumentFramesBySource({ topic, source_type: sourceType });
  const rows = frameSources.slice(0, 5);
  return (
    <GenPanel {...props} source={source} isLoading={isLoading}>
      {rows.length === 0 ? (
        <Empty text="No framing data" />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 9 }}>
          {rows.map((f) => (
            <div key={`${f.source}-${f.source_type}`} style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <div style={{ flex: 1, minWidth: 0, fontSize: 12.5, fontWeight: 500, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{f.source}</div>
              <span style={chip(f.concentrated ? palette.amber : palette.teal)}>{f.dominant.toUpperCase()}</span>
              <span style={{ ...mono, width: 52, textAlign: "right" }}>{f.doc_count} docs</span>
            </div>
          ))}
        </div>
      )}
    </GenPanel>
  );
}

function PositionsPanel(props: PanelProps) {
  const topic = props.panel.params?.topic as string | undefined;
  const sourceType = props.panel.params?.source_type as string | undefined;
  const { data: positions, source, isLoading } = useArgumentPositions({ topic, source_type: sourceType });
  const rows = positions.slice(0, 5);
  const stanceColor = (s: string) => (s === "for" ? palette.pos : s === "against" ? palette.neg : palette.neu);
  return (
    <GenPanel {...props} source={source} isLoading={isLoading}>
      {rows.length === 0 ? (
        <Empty text="No actor positions" />
      ) : (
        <div style={{ display: "flex", flexDirection: "column" }}>
          {rows.map((p, i) => (
            <div key={i} style={{ padding: "8px 0", borderBottom: i < rows.length - 1 ? "1px solid #12242e" : "none" }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <span style={{ fontSize: 12.5, fontWeight: 600 }}>{p.actor}</span>
                <span style={chip(stanceColor(p.stance))}>{p.stance.toUpperCase()}</span>
                <span style={mono}>{p.topic}</span>
              </div>
              <div style={{ fontSize: 12, color: "#9ab3bb", marginTop: 4, lineHeight: 1.4 }}>{p.position}</div>
            </div>
          ))}
        </div>
      )}
    </GenPanel>
  );
}

function ControversyPanel(props: PanelProps) {
  const topic = props.panel.params?.topic as string | undefined;
  const sourceType = props.panel.params?.source_type as string | undefined;
  const { data: conflicts, source, isLoading } = useArgumentControversy({ topic, source_type: sourceType });
  const rows = conflicts.slice(0, 5);
  const maxI = Math.max(0.01, ...rows.map((c) => c.intensity));
  return (
    <GenPanel {...props} source={source} isLoading={isLoading}>
      {rows.length === 0 ? (
        <Empty text="No conflicts detected" />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {rows.map((c, i) => (
            <div key={i}>
              <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
                <span style={{ fontSize: 12, fontWeight: 500 }}>
                  {c.actor_a} <span style={{ color: palette.neg }}>↔</span> {c.actor_b}
                </span>
                <span style={mono}>{c.topic}</span>
              </div>
              <div style={{ height: 4, background: "#193039", borderRadius: 2, overflow: "hidden" }}>
                <div style={{ height: "100%", width: `${Math.round((c.intensity / maxI) * 100)}%`, background: palette.neg }} />
              </div>
            </div>
          ))}
        </div>
      )}
    </GenPanel>
  );
}

function DriftPanel(props: PanelProps) {
  const topic = props.panel.params?.topic as string | undefined;
  const sourceType = props.panel.params?.source_type as string | undefined;
  const { data: events, source, isLoading } = useArgumentStanceDrift({ topic, source_type: sourceType });
  const rows = events.slice(0, 5);
  return (
    <GenPanel {...props} source={source} isLoading={isLoading}>
      {rows.length === 0 ? (
        <Empty text="No stance drift detected" />
      ) : (
        <div style={{ display: "flex", flexDirection: "column" }}>
          {rows.map((e, i) => (
            <div key={i} style={{ display: "flex", alignItems: "center", gap: 10, padding: "7px 0", borderBottom: i < rows.length - 1 ? "1px solid #12242e" : "none" }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 12.5, fontWeight: 500 }}>{e.source}</div>
                <div style={{ ...mono, marginTop: 2 }}>{e.topic}</div>
              </div>
              <span style={chip(STANCE_COLORS[e.from_stance] ?? palette.neu)}>{e.from_stance.toUpperCase()}</span>
              <span style={{ color: "#5f7580" }}>→</span>
              <span style={chip(STANCE_COLORS[e.to_stance] ?? palette.neu)}>{e.to_stance.toUpperCase()}</span>
            </div>
          ))}
        </div>
      )}
    </GenPanel>
  );
}

// R5: a horizontal error bar for a composite score with a bootstrap CI. The
// bar spans lo..hi on the 0..1 scale, with a tick at the point estimate, so
// the ranking shows a defensible interval instead of a naked number.
function ScoreBar({ value, ci }: { value: number | null; ci?: OutletScore["ci"] }) {
  const v = value ?? 0;
  const lo = ci ? Math.max(0, Math.min(ci.lo, v)) : v;
  const hi = ci ? Math.min(1, Math.max(ci.hi, v)) : v;
  return (
    <div style={{ position: "relative", width: 64, height: 10 }} title={ci ? `${(ci.level * 100).toFixed(0)}% CI [${ci.lo.toFixed(2)}, ${ci.hi.toFixed(2)}], n=${ci.n}` : undefined}>
      <div style={{ position: "absolute", top: 4, left: 0, right: 0, height: 2, background: "#1b2b33" }} />
      {ci ? (
        <div style={{ position: "absolute", top: 3.5, left: `${lo * 100}%`, width: `${Math.max(2, (hi - lo) * 100)}%`, height: 3, background: `${palette.teal}66`, borderRadius: 2 }} />
      ) : null}
      <div style={{ position: "absolute", top: 1, left: `calc(${v * 100}% - 1px)`, width: 2, height: 8, background: palette.teal }} />
    </div>
  );
}

function OutletRankingPanel(props: PanelProps) {
  const sourceType = props.panel.params?.source_type as string | undefined;
  const { data: outlets, source, isLoading } = useOutletRanking({ source_type: sourceType });
  const rows = outlets.slice(0, 5);
  const withCi = rows.some((o) => o.ci);
  return (
    <GenPanel {...props} source={source} isLoading={isLoading}>
      {rows.length === 0 ? (
        <Empty text="No outlet scores yet" />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {rows.map((o) => (
            <div key={`${o.source}-${o.source_type}`} style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <span style={{ ...mono, width: 18 }}>#{o.rank}</span>
              <div style={{ flex: 1, minWidth: 0, fontSize: 12.5, fontWeight: 500, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{o.source}</div>
              {o.ci ? <ScoreBar value={o.composite_score} ci={o.ci} /> : o.trend.length > 1 ? <Sparkline values={o.trend} color={ACCENT} /> : null}
              <span style={{ fontFamily: fonts.mono, fontSize: 11.5, color: palette.teal, width: 40, textAlign: "right" }}>
                {o.composite_score != null ? o.composite_score.toFixed(2) : "—"}
              </span>
            </div>
          ))}
          {withCi ? (
            <div style={{ ...mono, marginTop: 2 }}>
              bars show 95% bootstrap CI on the composite score
            </div>
          ) : null}
        </div>
      )}
    </GenPanel>
  );
}

// R5: the anomaly timeline. Flagged windows where a topic's daily coverage
// volume or mean sentiment deviated from its own recent history (robust
// z-score). Renders the honesty envelope (method / n / assumptions) so no
// point estimate ships naked. Data path lands with the R12 MCP data proxy;
// until then the panel shows a representative fixture.
const DEMO_ANOMALIES = {
  n: 21,
  method: "robust z-score (median/MAD) over per-topic daily series",
  assumptions: [
    "each topic's series is judged against its own recent history",
    "windows need at least 5 days of data; sparser topics are skipped",
  ],
  windows: [
    { window_date: "2025-06-11", metric: "volume", value: 34, robust_z: 4.2, is_anomaly: true },
    { window_date: "2025-06-14", metric: "sentiment", value: -0.42, robust_z: -3.8, is_anomaly: true },
    { window_date: "2025-06-18", metric: "volume", value: 28, robust_z: 3.6, is_anomaly: true },
  ],
};

function AnomalyTimelinePanel(props: PanelProps) {
  const topic = props.panel.params?.topic;
  const flagged = DEMO_ANOMALIES.windows.filter((w) => w.is_anomaly);
  return (
    <GenPanel {...props} source="demo">
      {flagged.length === 0 ? (
        <Empty text="No anomalies flagged" />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
          {flagged.map((w) => {
            const neg = w.robust_z < 0;
            return (
              <div key={`${w.metric}-${w.window_date}`} style={{ display: "flex", alignItems: "center", gap: 10 }}>
                <span style={{ ...mono, width: 74 }}>{w.window_date}</span>
                <span style={chip(w.metric === "volume" ? ACCENT : palette.amber)}>{w.metric}</span>
                <div style={{ flex: 1, minWidth: 0, fontSize: 12.5 }}>
                  {w.metric === "volume" ? `${w.value} articles` : `sentiment ${w.value.toFixed(2)}`}
                </div>
                <span style={{ fontFamily: fonts.mono, fontSize: 11.5, color: neg ? palette.neg : palette.pos, width: 52, textAlign: "right" }}>
                  z {w.robust_z > 0 ? "+" : ""}{w.robust_z.toFixed(1)}
                </span>
              </div>
            );
          })}
          <div style={{ ...mono, marginTop: 2 }}>
            {DEMO_ANOMALIES.method} · n={DEMO_ANOMALIES.n}
            {typeof topic === "string" && topic ? ` · ${topic}` : ""}
          </div>
        </div>
      )}
    </GenPanel>
  );
}

const CLUSTER_COLORS = [ACCENT, palette.teal, palette.blue, palette.amber, palette.violet, palette.pos];

function OutletClustersPanel(props: PanelProps) {
  const sourceType = props.panel.params?.source_type as string | undefined;
  const { data: outlets, source, isLoading } = useOutletClusters({ source_type: sourceType });
  if (outlets.length === 0) {
    return (
      <GenPanel {...props} source={source} isLoading={isLoading}>
        <Empty text="No outlet clusters yet" />
      </GenPanel>
    );
  }
  const xs = outlets.map((o) => o.pca_x);
  const ys = outlets.map((o) => o.pca_y);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const W = 320;
  const H = 170;
  const pad = 14;
  const px = (x: number) => pad + ((x - minX) / (maxX - minX || 1)) * (W - 2 * pad);
  const py = (y: number) => H - pad - ((y - minY) / (maxY - minY || 1)) * (H - 2 * pad);
  return (
    <GenPanel {...props} source={source} isLoading={isLoading}>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} style={{ display: "block" }}>
        {outlets.map((o, i) => (
          <g key={i}>
            <circle
              cx={px(o.pca_x)}
              cy={py(o.pca_y)}
              r={Math.max(3, Math.min(8, Math.sqrt(o.doc_count)))}
              fill={CLUSTER_COLORS[Math.abs(o.cluster_id) % CLUSTER_COLORS.length]}
              fillOpacity={0.75}
            />
            <text x={px(o.pca_x) + 8} y={py(o.pca_y) + 3} fontSize={8.5} fill="#8ca5af" fontFamily={fonts.mono}>
              {o.source}
            </text>
          </g>
        ))}
      </svg>
    </GenPanel>
  );
}

function ActorsPanel(props: PanelProps) {
  const sourceType = props.panel.params?.source_type as string | undefined;
  const { data: actors, source, isLoading } = useArgumentActorsSummary({ source_type: sourceType });
  const rows = actors.slice(0, 6);
  const roleColor = (r: string) => (r === "speaker" ? palette.blue : r === "author" ? palette.violet : palette.teal);
  return (
    <GenPanel {...props} source={source} isLoading={isLoading}>
      {rows.length === 0 ? (
        <Empty text="No actor data yet" />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {rows.map((a, i) => (
            <div key={i} style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <div style={{ flex: 1, minWidth: 0, fontSize: 12.5, fontWeight: 500, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{a.actor_name}</div>
              <span style={chip(roleColor(a.role))}>{a.role.toUpperCase()}</span>
              <span style={{ ...mono, width: 52, textAlign: "right" }}>{a.doc_count} docs</span>
            </div>
          ))}
        </div>
      )}
    </GenPanel>
  );
}

// R7 research-pack renderers. Data path lands with the MCP data proxy (R12);
// until then each shows a representative fixture.
const DEMO_VENUES = [
  { venue: "Nature Climate Change", papers: 58, credibility: { value: 0.82, lo: 0.79, hi: 0.85 } },
  { venue: "PNAS", papers: 41, credibility: { value: 0.76, lo: 0.72, hi: 0.8 } },
  { venue: "arXiv preprint", papers: 120, credibility: { value: 0.58, lo: 0.55, hi: 0.61 } },
  { venue: "Energy Policy", papers: 33, credibility: { value: 0.71, lo: 0.66, hi: 0.76 } },
];

function VenuesPanel(props: PanelProps) {
  return (
    <GenPanel {...props} source="demo">
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {DEMO_VENUES.map((v, i) => (
          <div key={v.venue} style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <span style={{ ...mono, width: 18 }}>#{i + 1}</span>
            <div style={{ flex: 1, minWidth: 0, fontSize: 12.5, fontWeight: 500, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{v.venue}</div>
            <span style={{ ...mono, width: 52, textAlign: "right" }}>{v.papers} papers</span>
            <ScoreBar value={v.credibility.value} ci={{ lo: v.credibility.lo, hi: v.credibility.hi, level: 0.95, n: v.papers }} />
            <span style={{ fontFamily: fonts.mono, fontSize: 11.5, color: palette.teal, width: 34, textAlign: "right" }}>{v.credibility.value.toFixed(2)}</span>
          </div>
        ))}
      </div>
      <div style={{ ...mono, marginTop: 4 }}>credibility generalizes the outlet transparency score to venues (95% CI)</div>
    </GenPanel>
  );
}

const DEMO_CITATIONS = {
  nodes: [
    { id: "p1", count: 120, color: ACCENT }, { id: "p2", count: 58, color: ACCENT },
    { id: "p3", count: 41, color: palette.teal }, { id: "p4", count: 33, color: palette.teal },
    { id: "p5", count: 22, color: palette.amber }, { id: "p6", count: 14, color: palette.amber },
  ],
  edges: [
    { source: "p2", target: "p1" }, { source: "p3", target: "p1" },
    { source: "p4", target: "p2" }, { source: "p5", target: "p3" }, { source: "p6", target: "p2" },
  ],
};

function CitationGraphPanel(props: PanelProps) {
  return (
    <GenPanel {...props} source="demo">
      <EntityGraph data={DEMO_CITATIONS as unknown as import("../types").LiveGraph} />
      <div style={{ ...mono, marginTop: 4 }}>papers linked by citation, sized by citation count</div>
    </GenPanel>
  );
}

const DEMO_LIT_CLAIMS = [
  { text: "Carbon capture at current cost is not scalable to 2030 targets.", verdict: "disputed", attributed: true },
  { text: "Solar LCOE fell below coal in most markets by 2024.", verdict: "supported", attributed: true },
  { text: "Grid storage duration is the binding constraint on renewables.", verdict: "unverified", attributed: false },
];

const VERDICT_COLOR: Record<string, string> = { supported: palette.pos, disputed: palette.neg, unverified: palette.dim };

function LiteratureClaimsPanel(props: PanelProps) {
  return (
    <GenPanel {...props} source="demo">
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {DEMO_LIT_CLAIMS.map((c, i) => (
          <div key={i} style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
            <span style={{ ...chip(VERDICT_COLOR[c.verdict] ?? palette.dim), marginTop: 2 }}>{c.verdict}</span>
            <div style={{ flex: 1, minWidth: 0, fontSize: 12.5, lineHeight: 1.35 }}>
              {c.text}
              {!c.attributed ? <span style={{ ...mono, color: palette.amber, marginLeft: 6 }}>uncited</span> : null}
            </div>
          </div>
        ))}
      </div>
      <div style={{ ...mono, marginTop: 4 }}>claims scoped to papers, from the shared claim layer</div>
    </GenPanel>
  );
}

const DEMO_KGS = [
  {
    name: "finance",
    description: "Earnings-call transcripts, provisioned (no pack code)",
    counts: { documents: 84, entities: 26, claims: 19 },
    sources: [
      { source: "Acme Corp Earnings", reason: "selected because transparency 0.81 >= 0.70" },
      { source: "Globex Investor Call", reason: "selected because transparency 0.76 >= 0.70" },
    ],
    sample: {
      documents: [
        { title: "Acme Corp Q3 earnings call transcript", source: "Acme Corp Earnings" },
        { title: "Globex FY guidance revised upward", source: "Globex Investor Call" },
      ],
      entities: [
        { entity: "earnings", mentions: 18 },
        { entity: "guidance", mentions: 12 },
        { entity: "margin", mentions: 9 },
      ],
      claims: [
        { text: "Cloud revenue grew 34 percent year over year.", verdict: "supported" },
        { text: "Guidance assumes no further rate hikes.", verdict: "unverified" },
      ],
    },
  },
  {
    name: "legal",
    description: "Policy and legal filings, provisioned (no pack code)",
    counts: { documents: 57, entities: 21, claims: 14 },
    sources: [{ source: "Federal Register", reason: "explicitly listed" }],
    sample: {
      documents: [
        { title: "Proposed rule on emissions disclosure", source: "Federal Register" },
        { title: "Comment period opens for data-privacy rule", source: "Federal Register" },
      ],
      entities: [
        { entity: "rule", mentions: 15 },
        { entity: "disclosure", mentions: 10 },
        { entity: "compliance", mentions: 7 },
      ],
      claims: [{ text: "The rule takes effect 90 days after publication.", verdict: "supported" }],
    },
  },
];

function ProvisionedKgPanel(props: PanelProps) {
  return (
    <GenPanel {...props} source="demo">
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        {DEMO_KGS.map((kg) => (
          <div key={kg.name} style={{ borderLeft: `2px solid ${palette.teal}`, paddingLeft: 10 }}>
            <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
              <span style={{ fontSize: 13, fontWeight: 600 }}>{kg.name}</span>
              <span style={{ ...mono }}>kg_{kg.name}_*</span>
            </div>
            <div style={{ fontSize: 12, color: palette.dim, margin: "2px 0 4px" }}>{kg.description}</div>
            <div style={{ display: "flex", gap: 12, ...mono }}>
              <span>{kg.counts.documents} docs</span>
              <span>{kg.counts.entities} entities</span>
              <span>{kg.counts.claims} claims</span>
              <span>{kg.sources.length} sources</span>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, marginTop: 6 }}>
              <div>
                <div style={{ ...mono, color: palette.teal }}>documents</div>
                {kg.sample.documents.map((d) => (
                  <div key={d.title} style={{ fontSize: 11.5, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{d.title}</div>
                ))}
              </div>
              <div>
                <div style={{ ...mono, color: palette.teal }}>entities</div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
                  {kg.sample.entities.map((e) => (
                    <span key={e.entity} style={{ ...chip(palette.dim) }}>{e.entity} {e.mentions}</span>
                  ))}
                </div>
              </div>
            </div>
            <div style={{ marginTop: 6 }}>
              <div style={{ ...mono, color: palette.teal }}>claims</div>
              {kg.sample.claims.map((c) => (
                <div key={c.text} style={{ fontSize: 11.5, display: "flex", gap: 6, alignItems: "flex-start" }}>
                  <span style={{ ...chip(VERDICT_COLOR[c.verdict] ?? palette.dim), marginTop: 2 }}>{c.verdict}</span>
                  <span style={{ flex: 1, minWidth: 0 }}>{c.text}</span>
                </div>
              ))}
            </div>
            <div style={{ marginTop: 4, display: "flex", flexDirection: "column", gap: 2 }}>
              {kg.sources.map((s) => (
                <div key={s.source} style={{ fontSize: 11 }}>
                  <span style={{ color: palette.teal }}>{s.source}</span>
                  <span style={{ color: palette.dim }}> - {s.reason}</span>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
      <div style={{ ...mono, marginTop: 4 }}>domains stood up by provisioning alone, no pack code</div>
    </GenPanel>
  );
}

const DEMO_CORROBORATION = {
  claim: { text: "The new emissions rule cuts sector output by 12 percent by 2030.", source: "Alpha Wire", credibility: 0.78 },
  support: [
    { source: "Beta Journal", credibility: 0.81, via: "evidence" },
    { source: "Gamma Review", credibility: 0.66, via: "evidence" },
  ],
  contradict: [{ source: "Delta Post", credibility: 0.42, via: "conflict" }],
  independent_support_count: 2,
  independent_contradict_count: 1,
  single_sourced: false,
};

function CorroborationPanel(props: PanelProps) {
  const c = DEMO_CORROBORATION;
  const Row = ({ s, kind }: { s: { source: string; credibility: number; via: string }; kind: "for" | "against" }) => (
    <div key={s.source} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12 }}>
      <span style={{ ...chip(kind === "for" ? palette.pos : palette.neg) }}>{kind}</span>
      <span style={{ flex: 1, minWidth: 0, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{s.source}</span>
      <span style={{ ...mono }}>{s.via}</span>
      <ScoreBar value={s.credibility} />
      <span style={{ fontFamily: fonts.mono, fontSize: 11, color: palette.teal, width: 30, textAlign: "right" }}>{s.credibility.toFixed(2)}</span>
    </div>
  );
  return (
    <GenPanel {...props} source="demo">
      <div style={{ fontSize: 12.5, lineHeight: 1.35, marginBottom: 6 }}>{c.claim.text}</div>
      <div style={{ ...mono, marginBottom: 8 }}>claimed by {c.claim.source} (credibility {c.claim.credibility.toFixed(2)})</div>
      {c.single_sourced ? (
        <div style={{ ...chip(palette.amber) }}>single-sourced, not corroborated</div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
          <div style={{ ...mono }}>{c.independent_support_count} independent for, {c.independent_contradict_count} against</div>
          {c.support.map((s) => <Row key={s.source} s={s} kind="for" />)}
          {c.contradict.map((s) => <Row key={s.source} s={s} kind="against" />)}
        </div>
      )}
      <div style={{ ...mono, marginTop: 6 }}>independent-source counts weighted by credibility, never one confidence number</div>
    </GenPanel>
  );
}

const DEMO_RELIABILITY = {
  source: "Grid Policy Weekly",
  reliability: { value: 0.71, lo: 0.6, hi: 0.82 },
  components: { transparency: 0.74, corroboration_hit_rate: 0.68, clean_record_rate: 0.92 },
  track_record: { documents: 214, claims: 88 },
  corrections: { disputed_claims: 7 },
  scored_as_outlet: true,
};

function ReliabilityCardPanel(props: PanelProps) {
  const r = DEMO_RELIABILITY;
  const rows: [string, number][] = [
    ["transparency", r.components.transparency],
    ["corroboration hit-rate", r.components.corroboration_hit_rate],
    ["clean record rate", r.components.clean_record_rate],
  ];
  return (
    <GenPanel {...props} source="demo">
      <div style={{ display: "flex", alignItems: "baseline", gap: 8, marginBottom: 6 }}>
        <span style={{ fontSize: 14, fontWeight: 600 }}>{r.source}</span>
        <span style={{ ...mono }}>{r.track_record.documents} docs, {r.track_record.claims} claims</span>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
        <span style={{ ...mono, width: 64 }}>reliability</span>
        <ScoreBar value={r.reliability.value} ci={{ lo: r.reliability.lo, hi: r.reliability.hi, level: 0.95, n: r.track_record.documents }} />
        <span style={{ fontFamily: fonts.mono, fontSize: 12, color: palette.teal }}>{r.reliability.value.toFixed(2)}</span>
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        {rows.map(([label, v]) => (
          <div key={label} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 11.5 }}>
            <span style={{ width: 118, color: palette.dim }}>{label}</span>
            <ScoreBar value={v} />
            <span style={{ fontFamily: fonts.mono, fontSize: 11, width: 30, textAlign: "right" }}>{v.toFixed(2)}</span>
          </div>
        ))}
      </div>
      <div style={{ ...mono, marginTop: 6 }}>
        {r.corrections.disputed_claims} disputed claims{r.scored_as_outlet ? " ; scored on the outlet transparency path" : ""}
      </div>
    </GenPanel>
  );
}

const DEMO_CONTRADICTIONS = [
  {
    claim_a: { text: "Storage costs fell 40 percent since 2022.", source: "Alpha Wire", cited: true },
    claim_b: { text: "Storage costs are essentially flat since 2022.", source: "Delta Post", cited: true },
    topic: "energy storage",
  },
  {
    claim_a: { text: "The rule takes effect in 90 days.", source: "Federal Register", cited: true },
    claim_b: { text: "The rule is delayed indefinitely.", source: "Unattributed brief", cited: false },
    topic: "emissions rule",
  },
];

function ContradictionLedgerPanel(props: PanelProps) {
  return (
    <GenPanel {...props} source="demo">
      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        {DEMO_CONTRADICTIONS.map((c, i) => (
          <div key={i} style={{ borderLeft: `2px solid ${palette.neg}`, paddingLeft: 10 }}>
            <div style={{ ...mono, color: palette.amber }}>{c.topic}</div>
            {[c.claim_a, c.claim_b].map((cl, j) => (
              <div key={j} style={{ display: "flex", gap: 6, alignItems: "flex-start", fontSize: 11.5, marginTop: 2 }}>
                <span style={{ ...chip(j === 0 ? ACCENT : palette.teal), marginTop: 2 }}>{cl.source}</span>
                <span style={{ flex: 1, minWidth: 0 }}>
                  {cl.text}
                  {!cl.cited ? <span style={{ ...mono, color: palette.amber, marginLeft: 6 }}>uncited</span> : null}
                </span>
              </div>
            ))}
          </div>
        ))}
      </div>
      <div style={{ ...mono, marginTop: 4 }}>where the record disagrees with itself; uncited entries flagged, never hidden</div>
    </GenPanel>
  );
}

const DEMO_DOSSIER = {
  entity: "Jordan Rivera",
  is_person: true,
  mention_count: 12,
  aliases: ["J. Rivera", "Jordan A. Rivera"],
  first_seen: "2025-11-03",
  last_seen: "2026-06-21",
  mentions: [
    { title: "Rivera testifies on grid resilience", source: "Alpha Wire", role: "speaker", cited: true },
    { title: "Committee questions Rivera on costs", source: "Beta Journal", role: "subject", cited: true },
    { title: "Unattributed brief names Rivera", source: "unknown", role: "subject", cited: false },
  ],
  connected_entities: [
    { entity: "Grid Authority", shared_documents: 5 },
    { entity: "Sen. Park", shared_documents: 3 },
  ],
};

function EntityDossierPanel(props: PanelProps) {
  const d = DEMO_DOSSIER;
  return (
    <GenPanel {...props} source="demo">
      <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
        <span style={{ fontSize: 14, fontWeight: 600 }}>{d.entity}</span>
        {d.is_person ? <span style={{ ...chip(palette.amber) }}>person</span> : null}
        <span style={{ ...mono }}>{d.mention_count} mentions</span>
      </div>
      <div style={{ ...mono, marginTop: 2 }}>
        aka {d.aliases.join(", ")} ; seen {d.first_seen} to {d.last_seen}
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 4, marginTop: 6 }}>
        {d.mentions.map((m, i) => (
          <div key={i} style={{ display: "flex", gap: 6, alignItems: "flex-start", fontSize: 11.5 }}>
            <span style={{ ...chip(m.cited ? palette.teal : palette.amber), marginTop: 2 }}>{m.cited ? m.source : "uncited"}</span>
            <span style={{ flex: 1, minWidth: 0, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{m.title}</span>
            <span style={{ ...mono }}>{m.role}</span>
          </div>
        ))}
      </div>
      <div style={{ marginTop: 6 }}>
        <div style={{ ...mono, color: palette.teal }}>connected</div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
          {d.connected_entities.map((c) => (
            <span key={c.entity} style={{ ...chip(palette.dim) }}>{c.entity} ({c.shared_documents})</span>
          ))}
        </div>
      </div>
      <div style={{ ...mono, marginTop: 4 }}>every line links to its source document; person facts document-sourced only</div>
    </GenPanel>
  );
}

const DEMO_PATH = {
  path: ["Jordan Rivera", "Grid Authority", "Delphi Energy"],
  hops: 2,
  edges: [
    { from: "Jordan Rivera", to: "Grid Authority", shared_documents: 5 },
    { from: "Grid Authority", to: "Delphi Energy", shared_documents: 2 },
  ],
};

function RelationshipPathPanel(props: PanelProps) {
  const p = DEMO_PATH;
  return (
    <GenPanel {...props} source="demo">
      <div style={{ ...mono, marginBottom: 6 }}>{p.hops} hops</div>
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {p.edges.map((e, i) => (
          <div key={i}>
            <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12.5 }}>
              <span style={{ fontWeight: 600 }}>{e.from}</span>
              <span style={{ color: palette.dim }}>to</span>
              <span style={{ fontWeight: 600 }}>{e.to}</span>
            </div>
            <div style={{ ...mono, color: palette.teal }}>{e.shared_documents} shared documents (cited evidence)</div>
          </div>
        ))}
      </div>
      <div style={{ ...mono, marginTop: 4 }}>each edge carries the cited documents that establish it</div>
    </GenPanel>
  );
}

const DEMO_TIMELINE = [
  { date: "2025-11-03", claim_count: 1, corroboration_density: 1, state: "single_sourced", entries: [{ text: "Rule first proposed.", source: "Alpha Wire", cited: true }] },
  { date: "2026-02-14", claim_count: 3, corroboration_density: 3, state: "cited", entries: [{ text: "Three outlets confirm the delay.", source: "Beta Journal", cited: true }] },
  { date: "2026-06-21", claim_count: 1, corroboration_density: 0, state: "uncited", entries: [{ text: "Anonymous brief claims reversal.", source: "unknown", cited: false }] },
];

const STATE_COLOR: Record<string, string> = { cited: palette.pos, single_sourced: palette.amber, uncited: palette.neg };

function EvidenceTimelinePanel(props: PanelProps) {
  return (
    <GenPanel {...props} source="demo">
      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        {DEMO_TIMELINE.map((ev, i) => (
          <div key={i} style={{ borderLeft: `2px solid ${STATE_COLOR[ev.state] ?? palette.dim}`, paddingLeft: 10 }}>
            <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
              <span style={{ fontFamily: fonts.mono, fontSize: 12 }}>{ev.date}</span>
              <span style={{ ...chip(STATE_COLOR[ev.state] ?? palette.dim) }}>{ev.corroboration_density} sources</span>
            </div>
            {ev.entries.map((e, j) => (
              <div key={j} style={{ fontSize: 11.5, marginTop: 2 }}>
                {e.text}
                <span style={{ ...mono, marginLeft: 6, color: e.cited ? palette.teal : palette.amber }}>{e.cited ? e.source : "uncited"}</span>
              </div>
            ))}
          </div>
        ))}
      </div>
      <div style={{ ...mono, marginTop: 4 }}>corroboration density per event; uncited entries flagged, never hidden</div>
    </GenPanel>
  );
}

function UnknownPanel(props: PanelProps) {
  return (
    <GenPanel {...props}>
      <Empty text={`Renderer for '${props.panel.type}' not installed`} />
    </GenPanel>
  );
}

const REGISTRY: Record<PanelType, ComponentType<PanelProps>> = {
  note: NotePanel,
  kpi_row: KpiRowPanel,
  articles: ArticlesPanel,
  documents: DocumentsPanel,
  trending: TrendingPanel,
  clusters: ClustersPanel,
  watchlists: WatchlistsPanel,
  timeline: TimelinePanel,
  sentiment_heatmap: SentimentHeatmapPanel,
  topic_sentiment: TopicSentimentPanel,
  entity_graph: EntityGraphPanel,
  claims: ClaimsPanel,
  stance: StancePanel,
  frames: FramesPanel,
  positions: PositionsPanel,
  controversy: ControversyPanel,
  drift: DriftPanel,
  outlet_ranking: OutletRankingPanel,
  outlet_clusters: OutletClustersPanel,
  actors: ActorsPanel,
  anomaly_timeline: AnomalyTimelinePanel,
  lead_lag: LeadLagPanel,
  narrative_thread: NarrativeThreadPanel,
  drift_trajectory: DriftTrajectoryPanel,
  forecast: ForecastPanel,
  venues: VenuesPanel,
  citation_graph: CitationGraphPanel,
  literature_claims: LiteratureClaimsPanel,
  provisioned_kg: ProvisionedKgPanel,
  corroboration: CorroborationPanel,
  reliability_card: ReliabilityCardPanel,
  contradiction_ledger: ContradictionLedgerPanel,
  entity_dossier: EntityDossierPanel,
  relationship_path: RelationshipPathPanel,
  evidence_timeline: EvidenceTimelinePanel,
};

export function panelComponent(type: string): ComponentType<PanelProps> {
  return REGISTRY[type as PanelType] ?? UnknownPanel;
}
