// Thin typed client over the NeuroNews FastAPI backend.
//
// In development, requests use relative paths and are proxied to the backend
// by Vite (see vite.config.ts). In other environments set VITE_API_BASE_URL
// to the absolute backend origin (e.g. https://api.neuronews.example).

const BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");
const DEFAULT_TIMEOUT_MS = 8000;

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, params?: Record<string, string | number | boolean | undefined>): Promise<T> {
  const url = new URL(BASE_URL + path, BASE_URL || window.location.origin);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined) url.searchParams.set(k, String(v));
    }
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), DEFAULT_TIMEOUT_MS);
  try {
    const res = await fetch(url.toString(), {
      headers: { Accept: "application/json" },
      signal: controller.signal,
    });
    if (!res.ok) {
      throw new ApiError(`Request failed: ${path}`, res.status);
    }
    return (await res.json()) as T;
  } catch (err) {
    if (err instanceof ApiError) throw err;
    throw new ApiError(err instanceof Error ? err.message : `Request error: ${path}`);
  } finally {
    clearTimeout(timer);
  }
}

async function requestPost<T>(path: string, body: unknown): Promise<T> {
  const url = new URL(BASE_URL + path, BASE_URL || window.location.origin);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), DEFAULT_TIMEOUT_MS);
  try {
    const res = await fetch(url.toString(), {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    if (!res.ok) {
      throw new ApiError(`Request failed: ${path}`, res.status);
    }
    return (await res.json()) as T;
  } catch (err) {
    if (err instanceof ApiError) throw err;
    throw new ApiError(err instanceof Error ? err.message : `Request error: ${path}`);
  } finally {
    clearTimeout(timer);
  }
}

// ---------- raw backend response shapes ----------

export interface RawArticle {
  id: string;
  title: string;
  url: string;
  publish_date: string | null;
  source: string;
  category: string;
  sentiment: { score: number | null; label: string | null };
}

export interface RawEventCluster {
  cluster_id: string;
  cluster_name: string;
  event_type: string;
  category: string;
  cluster_size: number;
  trending_score: number;
  impact_score: number;
  velocity_score: number;
  first_article_date: string;
  last_article_date: string;
  primary_sources: string[];
  key_entities: string[];
  status: string;
  avg_sentiment?: number | null;
  sample_headlines?: string[];
}

export interface RawBreakingNews {
  cluster_id: string;
  cluster_name: string;
  category: string;
  trending_score: number;
  sample_headlines: string | null;
}

export interface RawTrendingTopic {
  topic?: string;
  topic_name?: string;
  article_count: number;
  avg_probability?: number;
  avg_sentiment?: number;
  growth_rate?: number;
}

export interface RawTrendingResponse {
  trending_topics: RawTrendingTopic[];
}

export interface RawSentimentSummary {
  // shape varies; the adapter reads defensively
  [key: string]: unknown;
}

export interface RawHealth {
  status?: string;
  domain_packs?: { news?: boolean; [key: string]: boolean | undefined };
  [key: string]: unknown;
}

export interface RawDocument {
  document_id: string;
  source_type: string;
  title?: string | null;
  source_id?: string | null;
  url?: string | null;
  content?: string | null;
  created_at?: number | null;
  ingested_at: number;
  authors?: string[];
  metadata?: Record<string, unknown>;
}

export interface RawTopicSentiment {
  topic: string;
  total_articles: number;
  sentiments: Record<string, { count: number; avg_score: number; percentage?: number }>;
}

export interface RawKgStats {
  [key: string]: unknown;
}

export interface RawInfluencer {
  entity?: string;
  name?: string;
  entity_type?: string;
  type?: string;
  influence_score?: number;
  connections?: number;
  degree?: number;
}

export interface RawGraphNode {
  id: string;
  label: string;
  type: string;
  color: string;
  count: number;
  degree: number;
}

export interface RawEntityGraph {
  nodes: RawGraphNode[];
  edges: { source: string; target: string; weight: number }[];
  node_count: number;
  edge_count: number;
}

export interface RawHeatmap {
  topics: string[];
  cols: number;
  labels: string[];
  seed: number[][];
}

export interface RawFrameDistribution {
  distribution: Record<string, number>;
  dominant: string;
  total_documents: number;
  source_type_filter: string | null;
  source: string;
}

export interface RawClaim {
  claim_id: string;
  claim_text: string;
  document_id: string;
  source_type: string;
  confidence: number | null;
  extracted_at: string | null;
  factcheck_verdict: string | null;
  factcheck_url: string | null;
  factcheck_publisher: string | null;
  attributed: boolean | null;
  attribution_text: string | null;
}

export interface RawStanceSummary {
  topic: string;
  supportive: number;
  critical: number;
  neutral: number;
  ambiguous: number;
  total: number;
  drift: number[];
  by_source: Record<string, { supportive: number; critical: number; neutral: number; ambiguous: number }>;
}

export interface RawFrameSource {
  source: string;
  source_type: string;
  frames: Record<string, number>;
  doc_count: number;
  dominant: string;
  concentrated: boolean;
  concentrated_frame: string | null;
}

export interface RawDriftEvent {
  source: string;
  source_type: string;
  topic: string;
  from_stance: string;
  to_stance: string;
  confidence_delta: number | null;
  detected_at: string | null;
  window_pair: string | null;
}

export interface RawPositionUpdate {
  update_id: string;
  article_id: string;
  update_type: string;
  evidence_text: string;
  confidence: number;
  detected_at: string;
}

export interface RawActorPosition {
  actor: string;
  position: string;
  stance: string;
  date: string | null;
  source_type: string;
  document_id: string;
  topic: string;
  position_id?: string;
  updates?: RawPositionUpdate[];
}

export interface RawConflictPair {
  actor_a: string;
  actor_b: string;
  topic: string;
  intensity: number;
  source_count: number;
}

export interface RawControversyNode {
  id: string;
  label: string;
  source: string;
  source_type: string;
  topic: string;
  date: string | null;
  claim_text: string;
  confidence: number;
  document_id: string;
  conflict_type?: string;
}

export interface RawControversyEdge {
  source: string;
  target: string;
  severity: number;
  relation: string;
  conflict_type?: string;
}

export interface RawSourceRanking {
  source: string;
  source_type: string;
  claim_count: number;
  avg_confidence: number;
  evidence_citations: number;
}

export interface RawSourceStance {
  source: string;
  source_type: string;
  topic: string;
  supportive: number;
  critical: number;
  neutral: number;
  ambiguous: number;
  total: number;
  confidence: number | null;
  document_count: number;
  window_start: string | null;
  window_end: string | null;
}

export interface RawActor {
  document_id: string;
  source_type: string;
  actor_name: string;
  entity_id: string;
  role: "speaker" | "subject" | "author";
  confidence: number | null;
  extracted_at: string | null;
}

export interface RawActorSummary {
  actor_name: string;
  entity_id: string;
  role: "speaker" | "subject" | "author";
  doc_count: number;
  avg_confidence: number;
}

export interface RawOutletScore {
  rank: number;
  source: string;
  source_type: string;
  score_date: string;
  frame_diversity: number | null;
  attribution_rate: number | null;
  stance_neutrality: number | null;
  composite_score: number | null;
  doc_count: number;
  claim_count: number;
  trend: number[];
}

export interface RawOutletCluster {
  source: string;
  source_type: string;
  cluster_id: number;
  cluster_label: string;
  pca_x: number;
  pca_y: number;
  dominant_frame: string;
  doc_count: number;
  computed_at: string | null;
}

export interface RawUiSpecResponse {
  spec: import("../genui/spec").UISpec;
  meta: {
    generated_by: string;
    availability_known: boolean;
    ui_flags: Record<string, boolean>;
  };
}

export interface RawUiContext {
  ui_flags: Record<string, boolean>;
  availability: Record<string, boolean> | null;
  availability_known: boolean;
  llm: { enabled: boolean; provider: string | null };
}

// ---------- endpoint calls ----------

export const api = {
  health: () => request<RawHealth>("/health"),

  generateUi: (body: {
    intent: string;
    source_type?: string;
    signals?: { pinned: string[]; dismissed: string[]; weights: Record<string, number> };
  }) => requestPost<RawUiSpecResponse>("/api/v1/ui/generate", body),

  uiContext: () => request<RawUiContext>("/api/v1/ui/context"),

  articles: (params?: { category?: string; source?: string }) =>
    request<RawArticle[]>("/api/v1/news/articles", params),

  eventClusters: (params?: { limit?: number }) =>
    request<RawEventCluster[]>("/api/v1/events/clusters", params),

  breakingNews: (params?: { limit?: number; hours_back?: number }) =>
    request<RawBreakingNews[]>("/api/v1/breaking_news", params),

  trendingTopics: (params?: { days?: number }) =>
    request<RawTrendingResponse>("/topics/trending", params),

  sentimentSummary: () => request<RawSentimentSummary>("/news_sentiment/summary"),

  sentimentTopics: (params?: { days?: number; min_articles?: number }) =>
    request<RawTopicSentiment[]>("/news_sentiment/topics", params),

  knowledgeGraphStats: () => request<RawKgStats>("/api/v1/knowledge_graph_stats"),

  topInfluencers: (params?: { limit?: number }) =>
    request<RawInfluencer[]>("/api/influence/top-influencers", params),

  entityGraph: (params?: { days?: number; max_nodes?: number }) =>
    request<RawEntityGraph>("/api/v1/entity_graph", params),

  sentimentHeatmap: (params?: { days?: number; max_topics?: number }) =>
    request<RawHeatmap>("/news_sentiment/heatmap", params),

  documents: (params?: { source_type?: string; limit?: number }) =>
    request<RawDocument[]>("/api/v1/documents", params),

  packStatus: () => request<RawHealth>("/"),

  argumentFrames: (params?: { source_type?: string; document_id?: string; limit?: number }) =>
    request<RawFrameDistribution>("/api/v1/arguments/frames", params),

  argumentClaims: (params?: { document_id?: string; source_type?: string; topic?: string; unsourced_only?: boolean; limit?: number }) =>
    request<{ claims: RawClaim[]; count: number }>("/api/v1/arguments/claims", params),

  argumentStance: (params?: { topic?: string; source?: string; source_type?: string; date_range?: string }) =>
    request<{ stances: RawStanceSummary[]; count: number }>("/api/v1/arguments/stance", params),

  argumentPositions: (params?: { actor?: string; topic?: string; source_type?: string; limit?: number }) =>
    request<{ positions: RawActorPosition[]; count: number }>("/api/v1/arguments/positions", params),

  argumentControversy: (params?: { topic?: string; source_type?: string; date_range?: string; limit?: number }) =>
    request<{ conflicts: RawConflictPair[]; count: number }>("/api/v1/arguments/controversy", params),

  argumentControversyGraph: (params?: { topic?: string; source_type?: string; date_range?: string; limit?: number }) =>
    request<{ nodes: RawControversyNode[]; edges: RawControversyEdge[]; node_count: number; edge_count: number }>("/api/v1/arguments/controversy/graph", params),

  argumentSourcesRanking: (params?: { source_type?: string; limit?: number }) =>
    request<{ sources: RawSourceRanking[]; count: number }>("/api/v1/arguments/sources/ranking", params),

  argumentStanceSources: (params?: { topic?: string; source?: string; source_type?: string; date_range?: string; limit?: number }) =>
    request<{ sources: RawSourceStance[]; count: number }>("/api/v1/arguments/stance/sources", params),

  argumentStanceDrift: (params?: { source?: string; source_type?: string; topic?: string; limit?: number }) =>
    request<{ events: RawDriftEvent[]; drift: number[]; periods: string[]; count: number }>("/api/v1/arguments/stance/drift", params),

  argumentFramesBySource: (params?: { source?: string; source_type?: string; topic?: string; date_range?: string; limit?: number }) =>
    request<{ sources: RawFrameSource[]; count: number }>("/api/v1/arguments/frames/source", params),

  argumentActors: (params?: { document_id?: string; source_type?: string; role?: string; actor_name?: string; limit?: number }) =>
    request<{ actors: RawActor[]; count: number }>("/api/v1/arguments/actors", params),

  argumentActorsSummary: (params?: { source_type?: string; role?: string; limit?: number }) =>
    request<{ actors: RawActorSummary[]; count: number }>("/api/v1/arguments/actors/summary", params),

  argumentOutletClusters: (params?: { source_type?: string; cluster_id?: number; limit?: number }) =>
    request<{ outlets: RawOutletCluster[]; count: number }>("/api/v1/arguments/outlets/clusters", params),

  argumentOutletRanking: (params?: { source_type?: string; sort_by?: string; limit?: number }) =>
    request<{ outlets: RawOutletScore[]; count: number }>("/api/v1/arguments/outlets/ranking", params),
};
