// React Query hooks. Each hook attempts the live backend and transparently
// falls back to the design dataset when the API is unreachable, so the UI is
// always pixel-perfect and runnable. `source` reports which path was taken.

import { useQuery } from "@tanstack/react-query";
import { api } from "./api";
import {
  adaptArticles,
  adaptClusters,
  adaptTrending,
  adaptTopicSentiment,
  adaptEntityGraph,
  adaptHeatmap,
  adaptDocuments,
} from "./adapters";
import {
  mockArticles,
  mockClusters,
  mockDocuments,
  mockTrending,
  mockTickerText,
  mockTopicSentiment,
  mockHeatmap,
  mockClaims,
  mockStance,
  mockPositions,
  mockConflicts,
  mockControversyGraph,
  mockSourceStances,
  mockDriftEvents,
  mockFramesBySource,
  mockOutletRanking,
  mockOutletClusters,
} from "../data/mock";
import type { RawClaim, RawStanceSummary, RawActorPosition, RawPositionUpdate, RawSourceStance, RawDriftEvent, RawFrameSource, RawActorSummary, RawOutletCluster, RawOutletScore } from "./api";
import { palette, ACCENT } from "../theme";
import type {
  Article,
  Cluster,
  KnowledgeDocument,
  TrendingTopic,
  TopicSentiment,
  LiveGraph,
  Heatmap,
  ClaimResult,
  StanceSummary,
  ActorPosition,
  PositionUpdate,
  ConflictPair,
  FrameSource,
  SourceType,
  ControversyGraph,
  SourceStance,
  StanceDriftEvent,
  ActorSummary,
  OutletCluster,
  OutletScore,
} from "../types";

export type Source = "live" | "demo";
export interface Result<T> {
  data: T;
  source: Source;
  isLoading: boolean;
}

export type BackendStatus = "checking" | "online" | "offline";

const STALE = 60_000;

// Lightweight liveness probe against the backend's /health endpoint. Drives
// the global connection indicator in the top bar and is polled periodically so
// the UI reflects the backend coming up or going down without a reload.
export function useBackendStatus(): BackendStatus {
  const q = useQuery({
    queryKey: ["health"],
    queryFn: async () => {
      const h = await api.health();
      return h?.status ?? "ok";
    },
    staleTime: 15_000,
    refetchInterval: 30_000,
    retry: false,
  });
  if (q.isLoading) return "checking";
  return q.isSuccess ? "online" : "offline";
}

function useWithFallback<T>(key: string, fn: () => Promise<T>, fallback: T): Result<T> {
  const q = useQuery({
    queryKey: [key],
    queryFn: async (): Promise<{ data: T; source: Source }> => {
      try {
        const data = await fn();
        // Treat an empty payload as "no live data" and prefer the demo set.
        if (Array.isArray(data) && data.length === 0) {
          return { data: fallback, source: "demo" };
        }
        return { data, source: "live" };
      } catch {
        return { data: fallback, source: "demo" };
      }
    },
    staleTime: STALE,
    retry: false,
  });
  return {
    data: q.data?.data ?? fallback,
    source: q.data?.source ?? "demo",
    isLoading: q.isLoading,
  };
}

export function useArticles(): Result<Article[]> {
  return useWithFallback("articles", async () => adaptArticles(await api.articles()), mockArticles);
}

export function useClusters(): Result<Cluster[]> {
  return useWithFallback("clusters", async () => adaptClusters(await api.eventClusters()), mockClusters);
}

export function useTrending(params?: { days?: number }): Result<TrendingTopic[]> {
  return useWithFallback(
    `trending-${params?.days ?? "default"}`,
    async () => adaptTrending(await api.trendingTopics(params?.days ? { days: params.days } : undefined)),
    mockTrending,
  );
}

const mockEntityGraph: LiveGraph = {
  nodes: [
    { id: "fed", label: "Federal Reserve", type: "org", color: ACCENT, count: 9, degree: 4 },
    { id: "powell", label: "Jerome Powell", type: "person", color: palette.blue, count: 5, degree: 2 },
    { id: "nvda", label: "Nvidia", type: "org", color: ACCENT, count: 8, degree: 3 },
    { id: "huang", label: "Jensen Huang", type: "person", color: palette.blue, count: 4, degree: 1 },
    { id: "ai", label: "AI", type: "topic", color: palette.amber, count: 11, degree: 4 },
    { id: "eu", label: "European Union", type: "org", color: ACCENT, count: 7, degree: 3 },
    { id: "msft", label: "Microsoft", type: "org", color: ACCENT, count: 6, degree: 2 },
    { id: "opec", label: "OPEC", type: "org", color: ACCENT, count: 5, degree: 1 },
    { id: "oil", label: "Crude Oil", type: "topic", color: palette.amber, count: 5, degree: 1 },
    { id: "ukraine", label: "Ukraine", type: "place", color: palette.violet, count: 6, degree: 2 },
  ],
  edges: [
    ["fed", "powell", 5], ["fed", "ai", 3], ["nvda", "huang", 4], ["nvda", "ai", 6],
    ["ai", "msft", 3], ["eu", "msft", 2], ["eu", "ukraine", 4], ["opec", "oil", 5],
    ["nvda", "fed", 2], ["ai", "eu", 2],
  ],
  nodeCount: 10,
  edgeCount: 10,
};

export function useEntityGraph(params?: { days?: number }): Result<LiveGraph> {
  const days = params?.days ?? 7;
  return useWithFallback(
    `entityGraph-${days}`,
    async () => {
      const g = adaptEntityGraph(await api.entityGraph({ days, max_nodes: 16 }));
      if (!g.nodes.length) throw new Error("empty");
      return g;
    },
    mockEntityGraph,
  );
}

export function useTopicSentiment(params?: { days?: number }): Result<TopicSentiment[]> {
  const days = params?.days ?? 7;
  return useWithFallback(
    `topicSentiment-${days}`,
    async () => adaptTopicSentiment(await api.sentimentTopics({ days })),
    mockTopicSentiment,
  );
}

const mockSentimentHeatmap: Heatmap = {
  topics: ["Economy", "Technology", "Energy", "Policy", "Health", "Markets"],
  cols: mockHeatmap.cols,
  labels: Array.from({ length: mockHeatmap.cols }, (_, i) =>
    i % 2 === 0 ? String(i) : "",
  ),
  seed: mockHeatmap.seed,
};

export function useSentimentHeatmap(params?: { days?: number }): Result<Heatmap> {
  const days = params?.days ?? 14;
  return useWithFallback(
    `sentimentHeatmap-${days}`,
    async () => {
      const hm = adaptHeatmap(await api.sentimentHeatmap({ days }));
      if (!hm.topics.length) throw new Error("empty");
      return hm;
    },
    mockSentimentHeatmap,
  );
}

export function useDocuments(sourceType?: string): Result<KnowledgeDocument[]> {
  return useWithFallback(
    `documents-${sourceType ?? "all"}`,
    async () => {
      const raw = await api.documents(sourceType ? { source_type: sourceType } : undefined);
      return adaptDocuments(raw);
    },
    sourceType
      ? mockDocuments.filter((d) => d.source_type === sourceType)
      : mockDocuments,
  );
}

export function usePackStatus(): { newsPack: boolean; isLoading: boolean } {
  const q = useQuery({
    queryKey: ["packStatus"],
    queryFn: async () => {
      const root = await api.packStatus();
      return { newsPack: root.domain_packs?.news ?? true };
    },
    staleTime: 60_000,
    retry: false,
  });
  // Default to news-pack enabled so views render while checking or when offline.
  return { newsPack: q.data?.newsPack ?? true, isLoading: q.isLoading };
}

export function useTicker(): Result<string> {
  return useWithFallback(
    "ticker",
    async () => {
      const news = await api.breakingNews({ limit: 6 });
      if (!news.length) throw new Error("empty");
      const text =
        "  " +
        news
          .map((n) => n.sample_headlines?.split("|")[0]?.trim() || n.cluster_name)
          .filter(Boolean)
          .join("  ●  ");
      return `  ●${text}  `;
    },
    mockTickerText,
  );
}

// ─── Argument Mining ─────────────────────────────────────────────────────────

export function useArgumentClaims(params?: { source_type?: string; topic?: string; unsourced_only?: boolean }): Result<ClaimResult[]> {
  const key = `argumentClaims-${params?.source_type ?? "all"}-${params?.topic ?? ""}-${params?.unsourced_only ?? false}`;
  return useWithFallback(
    key,
    async (): Promise<ClaimResult[]> => {
      const res = await api.argumentClaims(params);
      return res.claims.map((r: RawClaim) => ({
        document_id: r.document_id,
        source_type: r.source_type as SourceType,
        text: r.claim_text,
        is_claim: true,
        confidence: r.confidence ?? 0,
        factcheck_verdict: (r.factcheck_verdict as ClaimResult["factcheck_verdict"]) ?? null,
        factcheck_url: r.factcheck_url ?? null,
        factcheck_publisher: r.factcheck_publisher ?? null,
        title: "",
        attributed: r.attributed ?? null,
        attribution_text: r.attribution_text ?? null,
      }));
    },
    mockClaims,
  );
}

export function useArgumentStance(params?: { source_type?: string; topic?: string }): Result<StanceSummary[]> {
  const key = `argumentStance-${params?.source_type ?? "all"}-${params?.topic ?? ""}`;
  return useWithFallback(
    key,
    async (): Promise<StanceSummary[]> => {
      const res = await api.argumentStance(params);
      return res.stances.map((r: RawStanceSummary) => ({
        topic: r.topic,
        supportive: r.supportive,
        critical: r.critical,
        neutral: r.neutral,
        ambiguous: r.ambiguous,
        total: r.total,
        drift: r.drift,
        by_source: r.by_source as StanceSummary["by_source"],
      }));
    },
    mockStance,
  );
}

export function useArgumentPositions(params?: { actor?: string; topic?: string; source_type?: string }): Result<ActorPosition[]> {
  const key = `argumentPositions-${params?.source_type ?? "all"}-${params?.topic ?? ""}`;
  return useWithFallback(
    key,
    async (): Promise<ActorPosition[]> => {
      const res = await api.argumentPositions(params);
      return res.positions.map((r: RawActorPosition) => ({
        actor: r.actor,
        position: r.position,
        stance: r.stance as ActorPosition["stance"],
        date: r.date ?? "",
        source_type: r.source_type as SourceType,
        document_id: r.document_id,
        topic: r.topic,
        position_id: r.position_id,
        updates: (r.updates ?? []).map((u: RawPositionUpdate): PositionUpdate => ({
          update_id:     u.update_id,
          article_id:    u.article_id,
          update_type:   u.update_type as PositionUpdate["update_type"],
          evidence_text: u.evidence_text,
          confidence:    u.confidence,
          detected_at:   u.detected_at,
        })),
      }));
    },
    mockPositions,
  );
}

export function useArgumentControversy(params?: { topic?: string; source_type?: string }): Result<ConflictPair[]> {
  const key = `argumentControversy-${params?.source_type ?? "all"}-${params?.topic ?? ""}`;
  return useWithFallback(
    key,
    async (): Promise<ConflictPair[]> => {
      const res = await api.argumentControversy(params);
      return res.conflicts;
    },
    mockConflicts,
  );
}

export function useArgumentControversyGraph(params?: { topic?: string; source_type?: string; date_range?: string }): Result<ControversyGraph> {
  const key = `argumentControversyGraph-${params?.source_type ?? "all"}-${params?.topic ?? ""}-${params?.date_range ?? ""}`;
  return useWithFallback(
    key,
    async (): Promise<ControversyGraph> => {
      const res = await api.argumentControversyGraph(params);
      if (!res.nodes.length) throw new Error("empty");
      return res;
    },
    mockControversyGraph,
  );
}

export function useArgumentStanceSources(params?: { topic?: string; source?: string; source_type?: string; date_range?: string }): Result<SourceStance[]> {
  const key = `argumentStanceSources-${params?.source_type ?? "all"}-${params?.topic ?? ""}`;
  return useWithFallback(
    key,
    async (): Promise<SourceStance[]> => {
      const res = await api.argumentStanceSources(params);
      return res.sources.map((r: RawSourceStance) => ({
        source: r.source,
        source_type: r.source_type as SourceType,
        topic: r.topic,
        supportive: r.supportive,
        critical: r.critical,
        neutral: r.neutral,
        ambiguous: r.ambiguous,
        total: r.total,
        confidence: r.confidence,
        document_count: r.document_count,
        window_start: r.window_start,
        window_end: r.window_end,
      }));
    },
    mockSourceStances,
  );
}

export function useArgumentStanceDrift(params?: { source?: string; source_type?: string; topic?: string }): Result<StanceDriftEvent[]> {
  const key = `argumentStanceDrift-${params?.source ?? ""}-${params?.source_type ?? "all"}-${params?.topic ?? ""}`;
  return useWithFallback(
    key,
    async (): Promise<StanceDriftEvent[]> => {
      const res = await api.argumentStanceDrift(params);
      if (!res.events || res.events.length === 0) throw new Error("empty");
      return res.events.map((r: RawDriftEvent) => ({
        source: r.source,
        source_type: r.source_type as SourceType,
        topic: r.topic,
        from_stance: r.from_stance as StanceDriftEvent["from_stance"],
        to_stance: r.to_stance as StanceDriftEvent["to_stance"],
        confidence_delta: r.confidence_delta,
        detected_at: r.detected_at,
        window_pair: r.window_pair,
      }));
    },
    mockDriftEvents,
  );
}

export function useArgumentFramesBySource(params?: { source?: string; source_type?: string; topic?: string; date_range?: string }): Result<FrameSource[]> {
  const key = `argumentFramesBySource-${params?.source_type ?? "all"}-${params?.topic ?? ""}-${params?.date_range ?? ""}`;
  return useWithFallback(
    key,
    async (): Promise<FrameSource[]> => {
      const res = await api.argumentFramesBySource(params);
      if (!res.sources || res.sources.length === 0) throw new Error("empty");
      return res.sources.map((r: RawFrameSource) => ({
        source: r.source,
        source_type: r.source_type as SourceType,
        frames: r.frames,
        doc_count: r.doc_count,
        dominant: r.dominant,
        concentrated: r.concentrated,
        concentrated_frame: r.concentrated_frame,
      }));
    },
    params?.source_type && params.source_type !== "all"
      ? mockFramesBySource.filter((s) => s.source_type === params.source_type)
      : mockFramesBySource,
  );
}

export function useOutletRanking(params?: { source_type?: string; sort_by?: string }): Result<OutletScore[]> {
  const key = `outletRanking-${params?.source_type ?? "all"}-${params?.sort_by ?? "composite_score"}`;
  const fallback = params?.source_type && params.source_type !== "all"
    ? mockOutletRanking.filter((o) => o.source_type === params.source_type)
    : mockOutletRanking;
  return useWithFallback(
    key,
    async (): Promise<OutletScore[]> => {
      const res = await api.argumentOutletRanking(params);
      if (!res.outlets.length) throw new Error("empty");
      return res.outlets.map((r: RawOutletScore) => ({
        rank:             r.rank,
        source:           r.source,
        source_type:      r.source_type as SourceType,
        score_date:       r.score_date,
        frame_diversity:  r.frame_diversity,
        attribution_rate: r.attribution_rate,
        stance_neutrality: r.stance_neutrality,
        composite_score:  r.composite_score,
        doc_count:        r.doc_count,
        claim_count:      r.claim_count,
        trend:            r.trend ?? [],
      }));
    },
    fallback,
  );
}

export function useOutletClusters(params?: { source_type?: string; cluster_id?: number }): Result<OutletCluster[]> {
  const key = `outletClusters-${params?.source_type ?? "all"}-${params?.cluster_id ?? ""}`;
  const fallback = params?.source_type && params.source_type !== "all"
    ? mockOutletClusters.filter((o) => o.source_type === params.source_type)
    : mockOutletClusters;
  return useWithFallback(
    key,
    async (): Promise<OutletCluster[]> => {
      const res = await api.argumentOutletClusters(params);
      if (!res.outlets.length) throw new Error("empty");
      return res.outlets.map((r: RawOutletCluster) => ({
        source:        r.source,
        source_type:   r.source_type as SourceType,
        cluster_id:    r.cluster_id,
        cluster_label: r.cluster_label,
        pca_x:         r.pca_x,
        pca_y:         r.pca_y,
        dominant_frame: r.dominant_frame,
        doc_count:     r.doc_count,
        computed_at:   r.computed_at,
      }));
    },
    fallback,
  );
}

export function useArgumentActorsSummary(params?: { source_type?: string; role?: string; limit?: number }): Result<ActorSummary[]> {
  const key = `argumentActorsSummary-${params?.source_type ?? "all"}-${params?.role ?? ""}`;
  return useWithFallback(
    key,
    async (): Promise<ActorSummary[]> => {
      const res = await api.argumentActorsSummary(params);
      return res.actors.map((r: RawActorSummary) => ({
        actor_name:     r.actor_name,
        entity_id:      r.entity_id,
        role:           r.role,
        doc_count:      r.doc_count,
        avg_confidence: r.avg_confidence,
      }));
    },
    [],
  );
}

