// Panel renderer registry — maps ui-spec-v1 panel types onto the app's
// existing hooks and chart components. Mirrors the backend catalog
// (src/genui/catalog.py); a spec panel type missing here renders a stub
// rather than crashing the canvas.

import type { CSSProperties, ComponentType } from "react";
import { ACCENT, palette, fonts } from "../theme";
import { sentColor, sentLabel } from "../lib/sentiment";
import {
  useArticles,
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
  const { data: articles, source, isLoading } = useArticles();
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

function EntityGraphPanel(props: PanelProps) {
  const { data, source, isLoading } = useEntityGraph({ days: daysParam(props.panel) });
  return (
    <GenPanel {...props} source={source} isLoading={isLoading}>
      <EntityGraph data={data} />
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

function OutletRankingPanel(props: PanelProps) {
  const sourceType = props.panel.params?.source_type as string | undefined;
  const { data: outlets, source, isLoading } = useOutletRanking({ source_type: sourceType });
  const rows = outlets.slice(0, 5);
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
              {o.trend.length > 1 ? <Sparkline values={o.trend} color={ACCENT} /> : null}
              <span style={{ fontFamily: fonts.mono, fontSize: 11.5, color: palette.teal, width: 40, textAlign: "right" }}>
                {o.composite_score != null ? o.composite_score.toFixed(2) : "—"}
              </span>
            </div>
          ))}
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
};

export function panelComponent(type: string): ComponentType<PanelProps> {
  return REGISTRY[type as PanelType] ?? UnknownPanel;
}
