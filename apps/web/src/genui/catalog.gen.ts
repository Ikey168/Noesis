// GENERATED FILE — DO NOT EDIT.
//
// Rendered from src/genui/catalog.py (the single source of truth for panel
// types, facets and layout/param metadata) by src/genui/codegen.py.
// Regenerate with:  python scripts/genui/codegen.py
// CI fails while this file is stale (tests/unit/genui/test_codegen.py).

export type PanelType =
  | "note"
  | "kpi_row"
  | "articles"
  | "documents"
  | "anomaly_timeline"
  | "trending"
  | "clusters"
  | "event_axis"
  | "watchlists"
  | "timeline"
  | "sentiment_heatmap"
  | "topic_sentiment"
  | "sentiment_trend"
  | "entity_graph"
  | "claims"
  | "stance"
  | "frames"
  | "positions"
  | "controversy"
  | "drift"
  | "outlet_ranking"
  | "outlet_clusters"
  | "actors"
  | "lead_lag"
  | "narrative_thread"
  | "drift_trajectory"
  | "forecast"
  | "venues"
  | "citation_graph"
  | "literature_claims"
  | "provisioned_kg"
  | "corroboration"
  | "reliability_card"
  | "contradiction_ledger"
  | "entity_dossier"
  | "relationship_path"
  | "evidence_timeline"
  | "provenance_trace";

export type Facet =
  | "overview"
  | "trend"
  | "sentiment"
  | "claims"
  | "stance"
  | "actors"
  | "conflict"
  | "sources"
  | "entities"
  | "events"
  | "library";

// Client-side view of the backend panel catalog: which facets each panel
// serves and how it takes its filters. Used by the offline fallback planner
// and to render pinned panels the spec omitted. The "note" panel is composed
// by the planners directly and is never selected from the catalog.
export interface PanelDef {
  type: PanelType;
  title: string;
  facets: Facet[];
  defaultSpan: number;
  topicParam?: boolean;
  sourceTypeParam?: boolean;
  daysParam?: boolean;
  // Upper bound of the endpoint's days validator (mirrors catalog.py).
  maxDays?: number;
}

export const PANEL_CATALOG: PanelDef[] = [
  { type: "kpi_row", title: "Signal summary", facets: ["overview"], defaultSpan: 12 },
  { type: "articles", title: "Latest documents", facets: ["overview", "sentiment"], defaultSpan: 6 },
  { type: "documents", title: "Library", facets: ["library", "overview"], defaultSpan: 6, sourceTypeParam: true },
  { type: "anomaly_timeline", title: "Anomaly timeline", facets: ["trend", "overview"], defaultSpan: 6, topicParam: true },
  { type: "trending", title: "Trending topics", facets: ["overview", "trend", "events"], defaultSpan: 6, daysParam: true, maxDays: 30 },
  { type: "clusters", title: "Event clusters", facets: ["overview", "events"], defaultSpan: 6 },
  { type: "event_axis", title: "Coverage timeline", facets: ["events", "trend"], defaultSpan: 6, topicParam: true, daysParam: true, maxDays: 90 },
  { type: "watchlists", title: "Watchlist", facets: ["events", "trend"], defaultSpan: 6 },
  { type: "timeline", title: "Story timeline", facets: ["events"], defaultSpan: 6, topicParam: true },
  { type: "sentiment_heatmap", title: "Sentiment heatmap", facets: ["sentiment", "trend"], defaultSpan: 6, daysParam: true, maxDays: 60 },
  { type: "topic_sentiment", title: "Sentiment by topic", facets: ["sentiment"], defaultSpan: 6, daysParam: true, maxDays: 90 },
  { type: "sentiment_trend", title: "Sentiment trajectory", facets: ["sentiment", "trend"], defaultSpan: 6, topicParam: true, daysParam: true, maxDays: 90 },
  { type: "entity_graph", title: "Entity graph", facets: ["entities", "actors", "overview"], defaultSpan: 6, daysParam: true, maxDays: 30 },
  { type: "claims", title: "Extracted claims", facets: ["claims", "conflict"], defaultSpan: 6, topicParam: true, sourceTypeParam: true },
  { type: "stance", title: "Stance breakdown", facets: ["stance", "conflict", "sentiment"], defaultSpan: 6, topicParam: true, sourceTypeParam: true },
  { type: "frames", title: "Framing by source", facets: ["sources", "claims"], defaultSpan: 6, topicParam: true, sourceTypeParam: true },
  { type: "positions", title: "Actor positions", facets: ["actors", "stance"], defaultSpan: 6, topicParam: true, sourceTypeParam: true },
  { type: "controversy", title: "Conflicts", facets: ["conflict", "claims"], defaultSpan: 6, topicParam: true, sourceTypeParam: true },
  { type: "drift", title: "Stance drift", facets: ["trend", "stance"], defaultSpan: 6, topicParam: true, sourceTypeParam: true },
  { type: "outlet_ranking", title: "Outlet transparency ranking", facets: ["sources"], defaultSpan: 6, sourceTypeParam: true },
  { type: "outlet_clusters", title: "Outlet clusters", facets: ["sources", "entities"], defaultSpan: 6, sourceTypeParam: true },
  { type: "actors", title: "Key actors", facets: ["actors", "entities"], defaultSpan: 6, sourceTypeParam: true },
  { type: "lead_lag", title: "Who leads, who follows", facets: ["sources", "trend"], defaultSpan: 6, topicParam: true },
  { type: "narrative_thread", title: "Narrative threads", facets: ["events", "overview"], defaultSpan: 6, topicParam: true, daysParam: true, maxDays: 90 },
  { type: "drift_trajectory", title: "Meaning drift", facets: ["trend"], defaultSpan: 6, topicParam: true },
  { type: "forecast", title: "Coverage forecast", facets: ["trend"], defaultSpan: 6, topicParam: true },
  { type: "venues", title: "Venue credibility", facets: ["sources", "library"], defaultSpan: 6 },
  { type: "citation_graph", title: "Citation graph", facets: ["entities", "library"], defaultSpan: 6, topicParam: true },
  { type: "literature_claims", title: "Literature claims", facets: ["claims", "library"], defaultSpan: 6, topicParam: true },
  { type: "provisioned_kg", title: "Provisioned knowledge graphs", facets: ["entities", "overview", "library"], defaultSpan: 6, topicParam: true },
  { type: "corroboration", title: "Claim corroboration", facets: ["claims", "sources", "conflict"], defaultSpan: 6 },
  { type: "reliability_card", title: "Source reliability", facets: ["sources"], defaultSpan: 6 },
  { type: "contradiction_ledger", title: "Contradiction ledger", facets: ["conflict", "claims"], defaultSpan: 6, topicParam: true },
  { type: "entity_dossier", title: "Entity dossier", facets: ["entities", "actors"], defaultSpan: 6, topicParam: true },
  { type: "relationship_path", title: "Connection path", facets: ["entities", "actors"], defaultSpan: 6 },
  { type: "evidence_timeline", title: "Evidence timeline", facets: ["events", "trend", "claims"], defaultSpan: 6, topicParam: true },
  { type: "provenance_trace", title: "Provenance trace", facets: ["claims", "sources", "library"], defaultSpan: 6, topicParam: true },
];

export const PANEL_DEFS: Partial<Record<PanelType, PanelDef>> = Object.fromEntries(
  PANEL_CATALOG.map((p) => [p.type, p]),
);
