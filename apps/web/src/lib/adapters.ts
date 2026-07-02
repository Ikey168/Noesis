// Map raw backend responses into the UI view-model shapes (types.ts).
// Adapters are deliberately defensive: the backend list endpoints expose a
// subset of the fields the design shows, so missing fields degrade gracefully.

import type {
  Article,
  Cluster,
  TrendingTopic,
  TopicSentiment,
  LiveGraph,
  Heatmap,
} from "../types";
import type {
  RawArticle,
  RawDocument,
  RawEventCluster,
  RawTrendingResponse,
  RawTopicSentiment,
  RawEntityGraph,
  RawHeatmap,
} from "./api";
import type { KnowledgeDocument, SourceType } from "../types";

export function adaptHeatmap(raw: RawHeatmap): Heatmap {
  return {
    topics: raw.topics ?? [],
    cols: raw.cols ?? 0,
    labels: raw.labels ?? [],
    seed: raw.seed ?? [],
  };
}

export function adaptEntityGraph(raw: RawEntityGraph): LiveGraph {
  const nodes = (raw.nodes ?? []).map((n) => ({
    id: n.id,
    label: n.label,
    type: n.type,
    color: n.color,
    count: n.count ?? 0,
    degree: n.degree ?? 0,
  }));
  const ids = new Set(nodes.map((n) => n.id));
  const edges = (raw.edges ?? [])
    .filter((e) => ids.has(e.source) && ids.has(e.target))
    .map((e) => [e.source, e.target, e.weight ?? 1] as [string, string, number]);
  return { nodes, edges, nodeCount: nodes.length, edgeCount: edges.length };
}

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const mins = Math.max(0, Math.round((Date.now() - then) / 60000));
  if (mins < 60) return `${mins}m`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h`;
  return `${Math.round(hrs / 24)}d`;
}

export function adaptArticles(raw: RawArticle[]): Article[] {
  return raw.map((a) => ({
    title: a.title,
    source: a.source ?? "—",
    time: relativeTime(a.publish_date),
    category: a.category ?? "General",
    sent: a.sentiment?.score ?? 0,
    summary: "",
    entities: [],
  }));
}

export function adaptClusters(raw: RawEventCluster[]): Cluster[] {
  return raw.map((c) => {
    const dir = c.velocity_score >= 0 ? "▲" : "▼";
    return {
      title: c.cluster_name,
      count: c.cluster_size,
      sources: c.primary_sources?.length ?? 0,
      time: relativeTime(c.last_article_date),
      sent: c.avg_sentiment ?? 0,
      vel: `${dir} ${Math.abs(Math.round(c.velocity_score * 100))}%/h`,
      headlines: c.sample_headlines ?? [],
    };
  });
}

export function adaptTrending(raw: RawTrendingResponse): TrendingTopic[] {
  const topics = raw.trending_topics ?? [];
  const max = Math.max(1, ...topics.map((t) => t.article_count ?? 0));
  return topics.map((t) => ({
    topic: t.topic ?? t.topic_name ?? "—",
    mentions: t.article_count ?? 0,
    // growth_rate is a fraction; scale to a percentage for the change column.
    change: Math.round((t.growth_rate ?? t.article_count / max) * 100),
    sent: t.avg_sentiment ?? 0,
  }));
}

export function adaptTopicSentiment(raw: RawTopicSentiment[]): TopicSentiment[] {
  return raw
    .map((t) => {
      // Collapse the per-label breakdown into one article-weighted average.
      const entries = Object.values(t.sentiments ?? {});
      const total = entries.reduce((s, e) => s + (e.count ?? 0), 0);
      const weighted = entries.reduce((s, e) => s + (e.count ?? 0) * (e.avg_score ?? 0), 0);
      return {
        topic: t.topic,
        articles: t.total_articles ?? total,
        avgScore: total > 0 ? weighted / total : 0,
      };
    })
    .sort((a, b) => b.articles - a.articles);
}


const VALID_SOURCE_TYPES = new Set(["news", "blog", "paper", "book", "transcript", "web", "note"]);

export function adaptDocuments(raw: RawDocument[]): KnowledgeDocument[] {
  return raw.map((d) => ({
    document_id: d.document_id,
    source_type: (VALID_SOURCE_TYPES.has(d.source_type) ? d.source_type : "web") as SourceType,
    title: d.title ?? null,
    source_id: d.source_id ?? null,
    url: d.url ?? null,
    content: d.content ?? null,
    created_at: d.created_at ?? null,
    ingested_at: d.ingested_at,
    authors: d.authors ?? [],
    metadata: d.metadata ?? {},
  }));
}

