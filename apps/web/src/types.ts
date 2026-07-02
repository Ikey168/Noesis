// View-model types consumed by the UI components. Backend responses are
// mapped into these shapes by the adapters in lib/adapters.ts.

export interface Article {
  title: string;
  source: string;
  time: string;
  category: string;
  sent: number;
  summary: string;
  entities: string[];
}

export interface Cluster {
  title: string;
  count: number;
  sources: number;
  time: string;
  sent: number;
  vel: string;
  headlines: string[];
}

export interface TrendingTopic {
  topic: string;
  mentions: number;
  change: number;
  sent: number;
}

export interface TopicSentiment {
  topic: string;
  avgScore: number;
  articles: number;
}

export interface Heatmap {
  topics: string[];
  cols: number;
  labels: string[];
  seed: number[][];
}

// Entity graph fetched live from the API (positions computed client-side).
export interface LiveGraphNode {
  id: string;
  label: string;
  type: string;
  color: string;
  count: number;
  degree: number;
}

export interface LiveGraph {
  nodes: LiveGraphNode[];
  edges: [string, string, number][];
  nodeCount: number;
  edgeCount: number;
}

export interface WatchItem {
  name: string;
  type: "Entity" | "Topic" | "Person";
  mentions: number;
  change: number;
  sent: number;
  spark: number[];
  alert: boolean;
}

export interface Story {
  id: string;
  label: string;
  sent: number;
}

export interface TimelineEvent {
  date: string;
  title: string;
  source: string;
  kind: "Origin" | "Development" | "Reaction" | "Milestone";
  sent: number;
}

export interface FrameSource {
  source: string;
  source_type: SourceType;
  frames: Record<string, number>;
  doc_count: number;
  dominant: string;
  concentrated: boolean;
  concentrated_frame: string | null;
}

export interface StanceDriftEvent {
  source: string;
  source_type: SourceType;
  topic: string;
  from_stance: "supportive" | "critical" | "neutral" | "ambiguous";
  to_stance: "supportive" | "critical" | "neutral" | "ambiguous";
  confidence_delta: number | null;
  detected_at: string | null;
  window_pair: string | null;
}

export interface ClaimResult {
  document_id: string;
  source_type: SourceType;
  text: string;
  is_claim: boolean;
  confidence: number;
  factcheck_verdict: "verified" | "disputed" | "mixed" | "unverified" | null;
  factcheck_url: string | null;
  factcheck_publisher: string | null;
  title: string;
  attributed: boolean | null;
  attribution_text: string | null;
}

export interface StanceSummary {
  topic: string;
  supportive: number;
  critical: number;
  neutral: number;
  ambiguous: number;
  total: number;
  by_source: Partial<Record<SourceType, { supportive: number; critical: number; neutral: number; ambiguous: number }>>;
  drift: number[];
}

export interface SourceStance {
  source: string;
  source_type: SourceType;
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


export type UpdateType = "reaffirmed" | "reversed" | "updated" | "no_signal";

export interface PositionUpdate {
  update_id: string;
  article_id: string;
  update_type: UpdateType;
  evidence_text: string;
  confidence: number;
  detected_at: string;
}

export interface ActorPosition {
  actor: string;
  position: string;
  stance: "for" | "against" | "neutral";
  date: string;
  source_type: SourceType;
  document_id: string;
  topic: string;
  position_id?: string;
  updates?: PositionUpdate[];
}

export interface ConflictPair {
  actor_a: string;
  actor_b: string;
  topic: string;
  intensity: number;
  source_count: number;
}

export interface ControversyNode {
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

export interface ControversyEdge {
  source: string;
  target: string;
  severity: number;
  relation: string;
  conflict_type?: string;
}

export interface ControversyGraph {
  nodes: ControversyNode[];
  edges: ControversyEdge[];
  node_count: number;
  edge_count: number;
}

export type SourceType = "news" | "blog" | "paper" | "book" | "transcript" | "web" | "note";


export interface ActorSummary {
  actor_name: string;
  entity_id: string;
  role: "speaker" | "subject" | "author";
  doc_count: number;
  avg_confidence: number;
}

export interface OutletScore {
  rank: number;
  source: string;
  source_type: SourceType;
  score_date: string;
  frame_diversity: number | null;
  attribution_rate: number | null;
  stance_neutrality: number | null;
  composite_score: number | null;
  doc_count: number;
  claim_count: number;
  trend: number[];
}

export interface OutletCluster {
  source: string;
  source_type: SourceType;
  cluster_id: number;
  cluster_label: string;
  pca_x: number;
  pca_y: number;
  dominant_frame: string;
  doc_count: number;
  computed_at: string | null;
}

export interface KnowledgeDocument {
  document_id: string;
  source_type: SourceType;
  title: string | null;
  source_id: string | null;
  url: string | null;
  content: string | null;
  created_at: number | null;
  ingested_at: number;
  authors: string[];
  metadata: Record<string, unknown>;
}
