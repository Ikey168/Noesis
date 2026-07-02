// Client-side fallback planner — a slim TypeScript mirror of the backend
// heuristic planner (src/genui/planner.py). Runs when the backend is
// unreachable so the canvas always generates, matching the app-wide
// demo-fallback convention. Kept rule-compatible: same facet keywords,
// same catalog-driven panel selection, same signal handling.

import type { Facet, PanelSpec, PanelType, UISpec } from "./spec";
import { PANEL_CATALOG, PANEL_DEFS } from "./spec";
import type { UsageSignals } from "./signals";

const FACET_KEYWORDS: Record<Facet, string[]> = {
  overview: [],
  trend: ["trend", "trends", "trending", "over time", "shift", "shifted", "evolution", "evolve", "evolved", "changing", "changed", "history", "momentum", "trajectory", "drift"],
  sentiment: ["sentiment", "tone", "mood", "feeling", "positive", "negative", "emotion", "optimism", "pessimism"],
  claims: ["claim", "claims", "fact", "facts", "factcheck", "fact-check", "verify", "verified", "unverified", "true", "false", "misinformation", "disinformation", "evidence", "assertion", "unsourced"],
  stance: ["stance", "stances", "support", "supports", "supportive", "oppose", "opposes", "opposition", "critical", "position", "positions", "agree", "agrees", "disagree", "disagrees", "pro", "against"],
  actors: ["who", "actor", "actors", "stakeholder", "stakeholders", "politician", "politicians", "speaker", "speakers", "people", "person", "says", "said", "voices"],
  conflict: ["conflict", "conflicts", "controversy", "controversial", "contradict", "contradicts", "contradiction", "contradictions", "dispute", "disputes", "disagreement", "clash", "debate"],
  sources: ["outlet", "outlets", "source", "sources", "bias", "biased", "framing", "frame", "frames", "coverage", "compare", "comparison", "transparency", "media", "publisher", "publishers"],
  entities: ["entity", "entities", "network", "graph", "connection", "connections", "related", "relationship", "relationships", "influence"],
  events: ["event", "events", "cluster", "clusters", "story", "stories", "breaking", "timeline", "happening", "developments", "watchlist", "watchlists", "alert", "alerts"],
  library: ["library", "document", "documents", "docs", "archive", "reading", "publications", "ingested", "corpus"],
};

const SOURCE_TYPE_WORDS: Record<string, string> = {
  news: "news",
  blog: "blog",
  blogs: "blog",
  paper: "paper",
  papers: "paper",
  book: "book",
  books: "book",
  transcript: "transcript",
  transcripts: "transcript",
  note: "note",
  notes: "note",
};

const TIME_WORDS: Record<string, number> = {
  today: 1,
  yesterday: 2,
  week: 7,
  weekly: 7,
  fortnight: 14,
  month: 30,
  monthly: 30,
  quarter: 90,
  year: 365,
};

const STOPWORDS = new Set(
  "a about an and are as at be been being between but by can concerning could did do does for from give had has have how i in into is it its last latest me my of on or our regarding show shows tell that the their them then there these this those to us versus vs was we were what when where which will with would you your recent past across".split(" "),
);

function tokenize(text: string): string[] {
  return text.toLowerCase().match(/[a-z0-9][a-z0-9'-]*/g) ?? [];
}

export function scoreFacets(intent: string): Partial<Record<Facet, number>> {
  const normalized = tokenize(intent).join(" ");
  const tokens = new Set(normalized.split(" "));
  const scores: Partial<Record<Facet, number>> = {};
  for (const [facet, keywords] of Object.entries(FACET_KEYWORDS) as [Facet, string[]][]) {
    let hits = 0;
    for (const kw of keywords) {
      if (kw.includes(" ") || kw.includes("-")) {
        // Substring match on the token-joined text, keyword as-is — the
        // tokenizer keeps hyphens, matching the backend planner exactly.
        if (normalized.includes(kw)) hits += 1;
      } else if (tokens.has(kw)) {
        hits += 1;
      }
    }
    if (hits) scores[facet] = hits;
  }
  return scores;
}

export function detectSourceType(intent: string): string | null {
  for (const token of tokenize(intent)) {
    if (SOURCE_TYPE_WORDS[token]) return SOURCE_TYPE_WORDS[token];
  }
  return null;
}

export function detectDays(intent: string): number | null {
  const m = intent.toLowerCase().match(/(\d+)\s*day/);
  if (m) return Math.max(1, Math.min(365, parseInt(m[1], 10)));
  for (const token of tokenize(intent)) {
    if (TIME_WORDS[token]) return TIME_WORDS[token];
  }
  return null;
}

const FACET_WORDS = new Set(
  Object.values(FACET_KEYWORDS)
    .flat()
    .flatMap((kw) => kw.replace("-", " ").split(" ")),
);

export function extractTopic(intent: string): string | null {
  const keep = tokenize(intent).filter(
    (t) =>
      !STOPWORDS.has(t) &&
      !FACET_WORDS.has(t) &&
      !SOURCE_TYPE_WORDS[t] &&
      !TIME_WORDS[t] &&
      !/^\d+$/.test(t) &&
      t !== "days",
  );
  return keep.length ? keep.slice(0, 5).join(" ") : null;
}

function titleCase(s: string): string {
  return s.replace(/\b[a-z]/g, (c) => c.toUpperCase());
}

export function localPlan(intent: string, signals: UsageSignals): UISpec {
  const trimmed = intent.slice(0, 500);
  const scores = scoreFacets(trimmed);
  let facets = (Object.entries(scores) as [Facet, number][])
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, 3)
    .map(([f]) => f);
  if (!facets.length) facets = ["overview"];

  const topic = extractTopic(trimmed);
  const sourceType = detectSourceType(trimmed);
  const days = detectDays(trimmed);

  // Facet-driven candidate selection, catalog order, deduplicated.
  const chosen = new Map<PanelType, PanelSpec>();
  facets.forEach((facet, rank) => {
    const weight = Math.max(0.4, 1 - 0.2 * rank);
    let position = 0;
    for (const def of PANEL_CATALOG) {
      if (!def.facets.includes(facet)) continue;
      const priority = Math.max(0.05, Math.min(1, weight - 0.05 * position));
      position += 1;
      const existing = chosen.get(def.type);
      if (!existing || priority > existing.priority) {
        chosen.set(def.type, {
          id: def.type,
          type: def.type,
          title: def.title,
          span: def.defaultSpan,
          priority,
          rationale: `selected for the '${facet}' facet`,
          params: {},
        });
      }
    }
  });

  // Usage signals: dismissals remove, pins add/boost, weights nudge.
  const pinned = new Set(signals.pinned);
  const dismissed = new Set(signals.dismissed.filter((t) => !pinned.has(t)));
  let panels = [...chosen.values()].filter((p) => !dismissed.has(p.type));
  for (const t of signals.pinned) {
    if (!panels.some((p) => p.type === t)) {
      const def = PANEL_DEFS[t];
      if (def) {
        panels.push({
          id: t,
          type: t,
          title: def.title,
          span: def.defaultSpan,
          priority: 0.6,
          rationale: "pinned by you",
          params: {},
        });
      }
    }
  }
  for (const p of panels) {
    if (pinned.has(p.type)) p.priority = Math.min(1, p.priority + 0.3);
    const w = signals.weights[p.type] ?? 0;
    if (w) p.priority = Math.min(1, p.priority + w * 0.01);
  }

  // Mirror the backend's empty-canvas guarantee: if muting removed every
  // panel, fall back to the overview set (minus muted types when possible).
  if (!panels.length) {
    const overview = PANEL_CATALOG.filter((def) => def.facets.includes("overview"));
    const usable = overview.filter((def) => !dismissed.has(def.type));
    panels = (usable.length ? usable : overview).map((def, i) => ({
      id: def.type,
      type: def.type,
      title: def.title,
      span: def.defaultSpan,
      priority: Math.max(0.05, 1 - 0.05 * i),
      rationale: "fallback overview (everything else muted)",
      params: {},
    }));
  }

  panels.sort((a, b) => b.priority - a.priority);
  panels = panels.slice(0, 11);

  for (const p of panels) {
    const def = PANEL_DEFS[p.type];
    if (!def) continue;
    p.params = p.params ?? {};
    if (topic && def.topicParam) p.params.topic = topic;
    if (sourceType && def.sourceTypeParam) p.params.source_type = sourceType;
    if (days && def.daysParam) p.params.days = def.maxDays ? Math.min(days, def.maxDays) : days;
  }

  const noteBits = [
    trimmed.trim() ? `Canvas generated for “${trimmed.trim()}”.` : "Default adaptive briefing canvas.",
    `Focus: ${facets.join(", ")}.`,
  ];
  if (topic) noteBits.push(`Topic filter: ${topic}.`);
  if (sourceType) noteBits.push(`Source type: ${sourceType}.`);
  if (days) noteBits.push(`Window: last ${days} days.`);
  noteBits.push("Planned locally — backend planner unreachable.");

  const note: PanelSpec = {
    id: "note",
    type: "note",
    title: "Plan",
    span: 12,
    priority: 1,
    rationale: "how this canvas was assembled",
    body: noteBits.join(" "),
  };

  const all = [note, ...panels].map((p, i) => ({ ...p, id: `p${i + 1}` }));
  const subtitleBits = [facets.join(", ")];
  if (sourceType) subtitleBits.push(sourceType);
  if (days) subtitleBits.push(`${days}d window`);

  return {
    spec_version: "ui-spec-v1",
    intent: trimmed,
    title: topic ? titleCase(topic) : "Adaptive Briefing",
    subtitle: subtitleBits.join(" · "),
    generated_by: "client",
    facets,
    topic,
    source_type: sourceType,
    panels: all,
  };
}
