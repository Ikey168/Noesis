// Data carried over verbatim from the design handoff. Used as a graceful
// fallback when a backend endpoint is unreachable, and as the source of truth
// for the watchlist / timeline panels that have no backend endpoint yet.

import type {
  Article,
  Cluster,
  TrendingTopic,
  TopicSentiment,
  WatchItem,
  Story,
  TimelineEvent,
  KnowledgeDocument,
  ClaimResult,
  StanceSummary,
  FrameSource,
  ActorPosition,
  ConflictPair,
  ControversyGraph,
  SourceStance,
  StanceDriftEvent,
  OutletCluster,
  OutletScore,
} from "../types";

const NOW = Date.now();
const HOUR = 3_600_000;

export const mockDocuments: KnowledgeDocument[] = [
  {
    document_id: "doc-news-001",
    source_type: "news",
    title: "Federal Reserve signals pause on rate hikes amid cooling inflation data",
    source_id: "Reuters",
    url: "https://reuters.com/article/fed-pause",
    content: "Policymakers indicated a willingness to hold rates steady as core PCE eased for a third consecutive month, lifting risk assets and easing pressure on equity markets.",
    created_at: NOW - 2 * 60_000,
    ingested_at: NOW - 90_000,
    authors: [],
    metadata: { category: "Economy" },
  },
  {
    document_id: "doc-paper-001",
    source_type: "paper",
    title: "Attention Is All You Need",
    source_id: "arxiv",
    url: "https://arxiv.org/abs/1706.03762",
    content: "We propose a new simple network architecture, the Transformer, based solely on attention mechanisms, dispensing with recurrence and convolutions entirely.",
    created_at: NOW - 30 * HOUR,
    ingested_at: NOW - 25 * HOUR,
    authors: ["Vaswani", "Shazeer", "Parmar", "Uszkoreit"],
    metadata: { arxiv_id: "1706.03762", primary_category: "cs.LG" },
  },
  {
    document_id: "doc-blog-001",
    source_type: "blog",
    title: "Scaling Laws for Neural Language Models",
    source_id: "OpenAI Blog",
    url: "https://openai.com/blog/scaling-laws",
    content: "We study empirical scaling laws for language model performance on cross-entropy loss. Loss scales as a power-law with model size, dataset size, and the amount of compute used for training.",
    created_at: NOW - 5 * HOUR,
    ingested_at: NOW - 4 * HOUR,
    authors: ["Kaplan et al."],
    metadata: { feed_name: "OpenAI Blog", tags: ["research", "tech"] },
  },
  {
    document_id: "doc-book-001",
    source_type: "book",
    title: "The Pragmatic Programmer",
    source_id: "Addison-Wesley",
    url: null,
    content: "From journeyman to master. A collection of tips and techniques for software development. Your Knowledge Portfolio, DRY principle, and orthogonality.",
    created_at: null,
    ingested_at: NOW - 48 * HOUR,
    authors: ["David Thomas", "Andrew Hunt"],
    metadata: { isbn: "9780135957059", chapter: "Chapter 1" },
  },
  {
    document_id: "doc-note-001",
    source_type: "note",
    title: "Meeting notes: AI strategy Q3",
    source_id: "upload",
    url: null,
    content: "Key decisions: (1) Prioritise LLM evaluation framework. (2) Deploy RAG pipeline to staging by end of month. (3) Review vector DB options.",
    created_at: NOW - 2 * HOUR,
    ingested_at: NOW - 2 * HOUR,
    authors: [],
    metadata: { format: "markdown" },
  },
  {
    document_id: "doc-web-001",
    source_type: "web",
    title: "What is Retrieval-Augmented Generation?",
    source_id: "huggingface.co",
    url: "https://huggingface.co/blog/rag",
    content: "Retrieval-Augmented Generation (RAG) is an AI framework for improving the quality of LLM responses by grounding generation in retrieved context from a knowledge base.",
    created_at: NOW - 12 * HOUR,
    ingested_at: NOW - 10 * HOUR,
    authors: [],
    metadata: {},
  },
];

export const mockArticles: Article[] = [
  { title: "Federal Reserve signals pause on rate hikes amid cooling inflation data", source: "Reuters", time: "2m", category: "Economy", sent: 0.34, summary: "Policymakers indicated a willingness to hold rates steady as core PCE eased for a third consecutive month, lifting risk assets.", entities: ["Federal Reserve", "Jerome Powell", "PCE"] },
  { title: "Nvidia unveils next-gen Blackwell Ultra accelerators at developer summit", source: "Bloomberg", time: "8m", category: "Technology", sent: 0.61, summary: "The chipmaker claims a 4x throughput gain for large-model inference, deepening its lead in the AI hardware race.", entities: ["Nvidia", "Jensen Huang", "AI"] },
  { title: "EU regulators open antitrust probe into cloud licensing practices", source: "FT", time: "14m", category: "Policy", sent: -0.42, summary: "Brussels is examining whether bundling terms unfairly disadvantage rival cloud providers across the bloc.", entities: ["European Union", "Microsoft", "Antitrust"] },
  { title: "Oil slips below $74 as OPEC+ weighs output adjustments", source: "CNBC", time: "21m", category: "Energy", sent: -0.18, summary: "Crude retreated on demand concerns ahead of the cartel’s ministerial meeting later this week.", entities: ["OPEC", "Saudi Arabia", "Crude Oil"] },
  { title: "Breakthrough fusion experiment sustains net energy gain for record duration", source: "Nature", time: "33m", category: "Science", sent: 0.72, summary: "Researchers report a stable burning plasma lasting several seconds, a milestone for commercial viability.", entities: ["Fusion", "LLNL", "Plasma"] },
  { title: "Major bank flags rising commercial real-estate delinquencies", source: "WSJ", time: "47m", category: "Finance", sent: -0.55, summary: "Quarterly filings show provisions for office-loan losses climbing as refinancing pressure mounts.", entities: ["JPMorgan", "CRE", "Credit"] },
  { title: "Pacific trade bloc finalizes digital-economy framework", source: "Nikkei", time: "1h", category: "Trade", sent: 0.28, summary: "Member states agreed on cross-border data flow rules and aligned standards for digital services.", entities: ["CPTPP", "Japan", "Trade"] },
  { title: "Pharma giant’s Alzheimer’s drug clears late-stage trial endpoint", source: "STAT", time: "1h", category: "Health", sent: 0.49, summary: "The therapy slowed cognitive decline by 27% versus placebo, sending shares sharply higher in pre-market.", entities: ["Eli Lilly", "FDA", "Alzheimer’s"] },
];

export const mockClusters: Cluster[] = [
  { title: "Global central banks converge on rate-pause signaling", count: 47, sources: 23, time: "12m", sent: 0.31, vel: "▲ 340%/h", headlines: ["Fed officials hint at extended pause", "ECB holds as inflation cools"] },
  { title: "AI chip supply chain tightens ahead of Q3 demand surge", count: 38, sources: 19, time: "24m", sent: 0.18, vel: "▲ 210%/h", headlines: ["Foundry capacity booked through Q4", "Memory prices climb on AI demand"] },
  { title: "Cloud antitrust scrutiny widens across EU and US", count: 31, sources: 17, time: "38m", sent: -0.39, vel: "▲ 155%/h", headlines: ["Regulators open new cloud probe", "Lawmakers press on lock-in practices"] },
  { title: "Energy markets react to OPEC+ output uncertainty", count: 29, sources: 21, time: "51m", sent: -0.22, vel: "▼ 40%/h", headlines: ["Crude swings on supply doubts", "Traders weigh demand outlook"] },
  { title: "Fusion milestone reignites clean-energy investment thesis", count: 22, sources: 14, time: "1h", sent: 0.66, vel: "▲ 480%/h", headlines: ["Net-energy result drives funding", "Startups race to commercialize"] },
  { title: "Commercial real-estate stress spreads to regional lenders", count: 26, sources: 16, time: "1h", sent: -0.51, vel: "▲ 90%/h", headlines: ["Office vacancies pressure balance sheets", "Refinancing wall looms in 2025"] },
];

export const mockTrending: TrendingTopic[] = [
  { topic: "Rate Pause", mentions: 4820, change: 340, sent: 0.31 },
  { topic: "Blackwell Ultra", mentions: 3910, change: 210, sent: 0.58 },
  { topic: "Fusion Energy", mentions: 2740, change: 480, sent: 0.66 },
  { topic: "Cloud Antitrust", mentions: 2310, change: 155, sent: -0.39 },
  { topic: "CRE Delinquency", mentions: 1980, change: 90, sent: -0.51 },
  { topic: "OPEC+ Output", mentions: 1720, change: -40, sent: -0.22 },
  { topic: "Alzheimer’s Trial", mentions: 1540, change: 120, sent: 0.49 },
  { topic: "Digital Trade Pact", mentions: 1180, change: 64, sent: 0.28 },
  { topic: "PCE Inflation", mentions: 980, change: 35, sent: 0.12 },
  { topic: "Regional Banks", mentions: 870, change: 72, sent: -0.44 },
];

// 24h hourly market-sentiment index used by the dashboard trend chart.

export const mockWatchlist: WatchItem[] = [
  { name: "Nvidia", type: "Entity", mentions: 38, change: 210, sent: 0.58, spark: [8, 10, 9, 14, 18, 22, 30, 38], alert: true },
  { name: "Federal Reserve", type: "Entity", mentions: 42, change: 12, sent: 0.31, spark: [30, 34, 33, 36, 38, 40, 41, 42], alert: false },
  { name: "Cloud Antitrust", type: "Topic", mentions: 31, change: 155, sent: -0.39, spark: [6, 8, 7, 12, 18, 24, 28, 31], alert: true },
  { name: "Fusion Energy", type: "Topic", mentions: 22, change: 480, sent: 0.66, spark: [2, 3, 2, 4, 7, 12, 18, 22], alert: true },
  { name: "OPEC+", type: "Topic", mentions: 17, change: -40, sent: -0.22, spark: [28, 26, 24, 22, 21, 19, 18, 17], alert: false },
  { name: "Jerome Powell", type: "Person", mentions: 19, change: 8, sent: 0.12, spark: [14, 16, 15, 17, 18, 18, 19, 19], alert: false },
];

export const mockStories: Story[] = [
  { id: "s1", label: "AI chip supply chain", sent: 0.18 },
  { id: "s2", label: "Fed rate-pause signal", sent: 0.31 },
  { id: "s3", label: "Cloud antitrust probe", sent: -0.39 },
];

export const mockTimeline: Record<string, TimelineEvent[]> = {
  s1: [
    { date: "Jun 04", title: "Reports surface of tightening CoWoS packaging capacity", source: "Nikkei", kind: "Origin", sent: -0.1 },
    { date: "Jun 07", title: "TSMC privately warns key clients of allocation cuts", source: "Reuters", kind: "Development", sent: -0.22 },
    { date: "Jun 11", title: "Hyperscalers reportedly pre-pay to secure 2026 supply", source: "Bloomberg", kind: "Development", sent: 0.05 },
    { date: "Jun 14", title: "TSMC raises capex guidance on advanced-node demand", source: "Reuters", kind: "Reaction", sent: 0.34 },
    { date: "Jun 18", title: "Nvidia unveils Blackwell Ultra; demand seen outpacing supply", source: "Bloomberg", kind: "Milestone", sent: 0.61 },
  ],
  s2: [
    { date: "Jun 02", title: "Core PCE eases for a second month", source: "CNBC", kind: "Origin", sent: 0.12 },
    { date: "Jun 09", title: "Labour market shows gradual cooling", source: "Bloomberg", kind: "Development", sent: 0.08 },
    { date: "Jun 13", title: "Two FOMC members flag upside inflation risk", source: "WSJ", kind: "Reaction", sent: -0.22 },
    { date: "Jun 18", title: "Fed signals pause as core PCE eases third month", source: "Reuters", kind: "Milestone", sent: 0.34 },
  ],
  s3: [
    { date: "Jun 06", title: "Complaints filed over cloud licensing bundling", source: "Politico", kind: "Origin", sent: -0.2 },
    { date: "Jun 12", title: "US agencies signal interest in cloud competition", source: "WSJ", kind: "Development", sent: -0.3 },
    { date: "Jun 18", title: "EU opens formal antitrust probe into cloud licensing", source: "FT", kind: "Milestone", sent: -0.42 },
  ],
};

// Heatmap seed (topics × 16 hourly columns) from the design.
export const mockHeatmap = {
  topics: ["Economy", "Technology", "Energy", "Policy", "Health", "Markets"],
  cols: 16,
  seed: [
    [0.1, 0.2, 0.0, -0.1, 0.1, 0.3, 0.2, 0.4, 0.3, 0.1, -0.1, 0.0, 0.2, 0.3, 0.4, 0.3],
    [0.4, 0.5, 0.3, 0.6, 0.5, 0.4, 0.6, 0.7, 0.5, 0.4, 0.5, 0.6, 0.4, 0.5, 0.6, 0.5],
    [-0.2, -0.3, -0.1, -0.4, -0.2, 0.0, -0.1, -0.3, -0.2, -0.4, -0.1, 0.0, -0.2, -0.3, -0.1, -0.2],
    [0.0, -0.2, -0.4, -0.3, -0.5, -0.2, -0.1, -0.3, -0.4, -0.2, 0.0, -0.1, -0.3, -0.4, -0.2, -0.4],
    [0.2, 0.3, 0.4, 0.3, 0.5, 0.4, 0.3, 0.5, 0.4, 0.6, 0.5, 0.4, 0.3, 0.5, 0.4, 0.5],
    [0.1, -0.1, 0.0, 0.2, -0.2, 0.1, -0.3, 0.0, 0.2, -0.1, 0.3, 0.1, -0.2, 0.0, 0.1, -0.1],
  ],
};

// Per-topic average sentiment (mirrors /news_sentiment/topics shape) used as a
// fallback for the Sentiment view's live topic breakdown.
export const mockTopicSentiment: TopicSentiment[] = [
  { topic: "Science", avgScore: 0.58, articles: 412 },
  { topic: "Technology", avgScore: 0.34, articles: 980 },
  { topic: "Health", avgScore: 0.27, articles: 356 },
  { topic: "Markets", avgScore: 0.04, articles: 642 },
  { topic: "Policy", avgScore: -0.19, articles: 528 },
  { topic: "Energy", avgScore: -0.31, articles: 274 },
  { topic: "Finance", avgScore: -0.44, articles: 489 },
];

export const mockTickerText =
  "  ●  Fed signals rate pause as core PCE eases  ●  Nvidia Blackwell Ultra claims 4x inference gain  ●  EU opens cloud antitrust probe  ●  Fusion experiment sustains net energy gain  ●  Oil slips below $74 on OPEC+ uncertainty  ●  Alzheimer’s drug clears late-stage trial  ";

// ─── Argument Mining ─────────────────────────────────────────────────────────

export const mockClaims: ClaimResult[] = [
  { document_id: "doc-news-001", source_type: "news",       title: "Federal Reserve signals pause on rate hikes",      text: "The unemployment rate fell to 3.8% in March, the lowest level in two decades.",                                                       is_claim: true,  confidence: 0.85, factcheck_verdict: "verified",   factcheck_url: "https://www.politifact.com/factchecks/2024/apr/05/claim-unemployment", factcheck_publisher: "PolitiFact",  attributed: true,  attribution_text: "Bureau of Labor Statistics" },
  { document_id: "doc-news-001", source_type: "news",       title: "Federal Reserve signals pause on rate hikes",      text: "Critics argue the government has not done enough to address the cost-of-living crisis.",                                             is_claim: false, confidence: 0.72, factcheck_verdict: null,        factcheck_url: null, factcheck_publisher: null,              attributed: true,  attribution_text: "Critics" },
  { document_id: "doc-news-002", source_type: "news",       title: "Nvidia Blackwell Ultra claims 4× inference gain",  text: "Blackwell Ultra delivers 4× the inference throughput of its predecessor at equal power.",                                       is_claim: true,  confidence: 0.91, factcheck_verdict: "disputed",   factcheck_url: "https://factcheck.org/2024/blackwell-4x-claim", factcheck_publisher: "FactCheck.org", attributed: false, attribution_text: null },
  { document_id: "doc-blog-001", source_type: "blog",       title: "Platform Changes Impact Analysis",                 text: "Engagement dropped 37% after the platform update.",                                                                               is_claim: true,  confidence: 0.78, factcheck_verdict: "mixed",      factcheck_url: null, factcheck_publisher: "Snopes",          attributed: false, attribution_text: null },
  { document_id: "doc-blog-001", source_type: "blog",       title: "Platform Changes Impact Analysis",                 text: "The library added async support in version 3.2, released on 4 April 2024.",                                                       is_claim: true,  confidence: 0.91, factcheck_verdict: "verified",   factcheck_url: null, factcheck_publisher: null,              attributed: false, attribution_text: null },
  { document_id: "doc-paper-001", source_type: "paper",     title: "Sleep Duration and Cognitive Performance",         text: "A statistically significant correlation between sleep duration and cognitive performance (r = 0.74, p < 0.001).",                is_claim: true,  confidence: 0.94, factcheck_verdict: "verified",   factcheck_url: null, factcheck_publisher: null,              attributed: true,  attribution_text: "(Hirsch et al., 2023)" },
  { document_id: "doc-paper-001", source_type: "paper",     title: "Sleep Duration and Cognitive Performance",         text: "The cohort comprised 3,247 participants aged 18–65 recruited across six clinical sites.",                                        is_claim: true,  confidence: 0.88, factcheck_verdict: "unverified", factcheck_url: null, factcheck_publisher: null,              attributed: false, attribution_text: null },
  { document_id: "doc-transcript-001", source_type: "transcript", title: "Q3 2026 Earnings Call",                     text: "We reported a 30% reduction in operating costs across all four divisions.",                                                       is_claim: true,  confidence: 0.82, factcheck_verdict: "disputed",   factcheck_url: "https://factcheck.org/2024/earnings-30pct", factcheck_publisher: "FactCheck.org", attributed: true,  attribution_text: "CFO Jane Doe" },
  { document_id: "doc-transcript-001", source_type: "transcript", title: "Q3 2026 Earnings Call",                     text: "The committee chair confirmed the vote passed by nine votes to three.",                                                           is_claim: true,  confidence: 0.89, factcheck_verdict: "verified",   factcheck_url: null, factcheck_publisher: null,              attributed: true,  attribution_text: "committee chair" },
  { document_id: "doc-book-001", source_type: "book",       title: "The Long Siege: A History",                       text: "By 1943 the city had lost more than a third of its pre-war population to evacuation.",                                           is_claim: true,  confidence: 0.77, factcheck_verdict: "unverified", factcheck_url: null, factcheck_publisher: null,              attributed: false, attribution_text: null },
  { document_id: "doc-book-001", source_type: "book",       title: "The Long Siege: A History",                       text: "The treaty signed on 11 June 1919 transferred sovereignty over the territory to the new republic.",                               is_claim: true,  confidence: 0.83, factcheck_verdict: "verified",   factcheck_url: null, factcheck_publisher: null,              attributed: true,  attribution_text: "[14]" },
  { document_id: "doc-note-001", source_type: "note",       title: "Project Alpha Status Note",                       text: "Board approved budget of $2.4M on 14 June; finance confirmed transfer completed same day.",                                        is_claim: true,  confidence: 0.76, factcheck_verdict: null,        factcheck_url: null, factcheck_publisher: null,              attributed: true,  attribution_text: "finance" },
  { document_id: "doc-note-001", source_type: "note",       title: "Project Alpha Status Note",                       text: "Security audit completed 10 June — 3 critical findings, all remediated by 12 June.",                                              is_claim: true,  confidence: 0.81, factcheck_verdict: null,        factcheck_url: null, factcheck_publisher: null,              attributed: false, attribution_text: null },
];

export const mockStance: StanceSummary[] = [
  { topic: "Interest Rate Policy",       supportive: 24, critical: 31, neutral: 18, ambiguous: 7,  total: 80, drift: [0.3, 0.28, 0.35, 0.38, 0.31, 0.29, 0.32], by_source: { news: { supportive: 12, critical: 18, neutral: 8,  ambiguous: 3 }, blog: { supportive: 6, critical: 7, neutral: 5, ambiguous: 2 }, paper: { supportive: 4, critical: 4, neutral: 3, ambiguous: 1 }, transcript: { supportive: 2, critical: 2, neutral: 2, ambiguous: 1 } } },
  { topic: "AI Regulation",              supportive: 38, critical: 19, neutral: 27, ambiguous: 12, total: 96, drift: [0.4, 0.42, 0.38, 0.44, 0.46, 0.41, 0.43], by_source: { news: { supportive: 18, critical: 9, neutral: 12, ambiguous: 5 }, blog: { supportive: 12, critical: 6, neutral: 8,  ambiguous: 4 }, paper: { supportive: 5, critical: 3, neutral: 5, ambiguous: 2 }, transcript: { supportive: 3, critical: 1, neutral: 2, ambiguous: 1 } } },
  { topic: "Climate Policy",             supportive: 41, critical: 22, neutral: 14, ambiguous: 8,  total: 85, drift: [0.45, 0.48, 0.5, 0.47, 0.52, 0.49, 0.51], by_source: { news: { supportive: 20, critical: 11, neutral: 7,  ambiguous: 4 }, blog: { supportive: 10, critical: 5, neutral: 4,  ambiguous: 2 }, paper: { supportive: 8, critical: 4, neutral: 2, ambiguous: 1 }, transcript: { supportive: 3, critical: 2, neutral: 1, ambiguous: 1 } } },
  { topic: "Healthcare Reform",          supportive: 29, critical: 34, neutral: 22, ambiguous: 10, total: 95, drift: [0.31, 0.29, 0.32, 0.27, 0.3, 0.28, 0.29], by_source: { news: { supportive: 14, critical: 17, neutral: 10, ambiguous: 5 }, blog: { supportive: 8, critical: 9, neutral: 6,  ambiguous: 3 }, paper: { supportive: 5, critical: 5, neutral: 4, ambiguous: 1 }, transcript: { supportive: 2, critical: 3, neutral: 2, ambiguous: 1 } } },
  { topic: "Trade Tariffs",              supportive: 15, critical: 48, neutral: 19, ambiguous: 6,  total: 88, drift: [0.17, 0.19, 0.16, 0.14, 0.18, 0.15, 0.16], by_source: { news: { supportive: 7, critical: 24, neutral: 9,  ambiguous: 3 }, blog: { supportive: 4, critical: 12, neutral: 5,  ambiguous: 2 }, paper: { supportive: 2, critical: 8, neutral: 3, ambiguous: 0 }, transcript: { supportive: 2, critical: 4, neutral: 2, ambiguous: 1 } } },
  { topic: "Central Bank Independence",  supportive: 33, critical: 12, neutral: 28, ambiguous: 5,  total: 78, drift: [0.42, 0.41, 0.44, 0.43, 0.45, 0.42, 0.44], by_source: { news: { supportive: 16, critical: 6, neutral: 14, ambiguous: 2 }, blog: { supportive: 9, critical: 3, neutral: 8,  ambiguous: 2 }, paper: { supportive: 6, critical: 2, neutral: 4, ambiguous: 1 }, transcript: { supportive: 2, critical: 1, neutral: 2, ambiguous: 0 } } },
];


export const mockPositions: ActorPosition[] = [
  {
    actor: "Federal Reserve", topic: "Interest Rate Policy",
    position: "Hold at 5.25%; monitor inflation before cutting",
    stance: "neutral", date: "2026-06-20", source_type: "news", document_id: "doc-news-001",
    position_id: "pos-mock-001",
    updates: [
      { update_id: "upd-mock-001a", article_id: "doc-news-101", update_type: "reaffirmed", evidence_text: "The Federal Reserve maintained its position on interest rates, reiterating its intention to hold until inflation data improves.", confidence: 0.82, detected_at: "2026-06-23T08:00:00Z" },
      { update_id: "upd-mock-001b", article_id: "doc-news-102", update_type: "updated",    evidence_text: "Federal Reserve minutes suggest the timeline for the first cut has been extended into Q4.", confidence: 0.68, detected_at: "2026-06-25T14:30:00Z" },
    ],
  },
  {
    actor: "IMF", topic: "Interest Rate Policy",
    position: "Advocates gradual cuts to prevent recession",
    stance: "for", date: "2026-06-18", source_type: "paper", document_id: "doc-paper-002",
    position_id: "pos-mock-002",
    updates: [
      { update_id: "upd-mock-002a", article_id: "doc-news-103", update_type: "reaffirmed", evidence_text: "IMF reiterated its call for gradual monetary easing at the G7 finance ministers meeting.", confidence: 0.76, detected_at: "2026-06-22T10:00:00Z" },
    ],
  },
  {
    actor: "Goldman Sachs", topic: "Interest Rate Policy",
    position: "Expects two cuts in H2; overweight equities",
    stance: "for", date: "2026-06-15", source_type: "transcript", document_id: "doc-transcript-001",
    position_id: "pos-mock-003",
    updates: [
      { update_id: "upd-mock-003a", article_id: "doc-news-104", update_type: "reversed",   evidence_text: "Goldman Sachs revised its forecast, no longer expecting two cuts; analysts backed away from the overweight equities call amid renewed inflation data.", confidence: 0.83, detected_at: "2026-06-24T09:15:00Z" },
    ],
  },
  {
    actor: "Senator Warren", topic: "Interest Rate Policy",
    position: "Criticises Fed; calls for political oversight",
    stance: "against", date: "2026-06-12", source_type: "news", document_id: "doc-news-003",
    position_id: "pos-mock-004",
    updates: [],
  },
  {
    actor: "BIS", topic: "Interest Rate Policy",
    position: "Warns premature cuts risk wage-price spiral",
    stance: "against", date: "2026-06-10", source_type: "paper", document_id: "doc-paper-003",
    position_id: "pos-mock-005",
    updates: [
      { update_id: "upd-mock-005a", article_id: "doc-news-105", update_type: "reaffirmed", evidence_text: "BIS general manager doubled down on warnings against premature rate cuts, citing persistent services inflation.", confidence: 0.79, detected_at: "2026-06-21T11:00:00Z" },
    ],
  },
  {
    actor: "ECB", topic: "Interest Rate Policy",
    position: "Diverging from Fed; began cutting cycle in June",
    stance: "for", date: "2026-06-08", source_type: "news", document_id: "doc-news-004",
    position_id: "pos-mock-006",
    updates: [
      { update_id: "upd-mock-006a", article_id: "doc-news-106", update_type: "updated",    evidence_text: "ECB adjusted its forward guidance, narrowing the expected pace of cuts from quarterly to semi-annual.", confidence: 0.65, detected_at: "2026-06-20T15:00:00Z" },
    ],
  },
  {
    actor: "EU Commission", topic: "AI Regulation",
    position: "Mandatory risk assessment for frontier models",
    stance: "for", date: "2026-06-19", source_type: "news", document_id: "doc-news-005",
    position_id: "pos-mock-007",
    updates: [
      { update_id: "upd-mock-007a", article_id: "doc-news-107", update_type: "updated",    evidence_text: "EU Commission expanded the scope of mandatory assessments to include open-weight models above 10B parameters.", confidence: 0.71, detected_at: "2026-06-24T13:00:00Z" },
    ],
  },
  {
    actor: "Tech Industry", topic: "AI Regulation",
    position: "Self-regulation preferred; opposes hard limits",
    stance: "against", date: "2026-06-17", source_type: "blog", document_id: "doc-blog-002",
    position_id: "pos-mock-008",
    updates: [],
  },
  {
    actor: "UN AI Advisory", topic: "AI Regulation",
    position: "International treaty with binding safety norms",
    stance: "for", date: "2026-06-14", source_type: "paper", document_id: "doc-paper-004",
    position_id: "pos-mock-009",
    updates: [
      { update_id: "upd-mock-009a", article_id: "doc-news-108", update_type: "reaffirmed", evidence_text: "UN AI advisory panel reiterated its stance on binding safety norms at the Vienna AI governance forum.", confidence: 0.74, detected_at: "2026-06-23T16:00:00Z" },
    ],
  },
];

export const mockConflicts: ConflictPair[] = [
  { actor_a: "Federal Reserve",  actor_b: "Senator Warren",   topic: "Interest Rate Policy",      intensity: 0.78, source_count: 14 },
  { actor_a: "Tech Industry",    actor_b: "EU Commission",    topic: "AI Regulation",              intensity: 0.91, source_count: 23 },
  { actor_a: "OPEC",             actor_b: "US Energy Dept",   topic: "Oil Production",             intensity: 0.64, source_count: 11 },
  { actor_a: "China",            actor_b: "WTO",              topic: "Trade Tariffs",              intensity: 0.83, source_count: 19 },
  { actor_a: "US Treasury",      actor_b: "IMF",              topic: "Dollar Strength",            intensity: 0.52, source_count: 8  },
  { actor_a: "WHO",              actor_b: "Pharma Industry",  topic: "Drug Pricing",               intensity: 0.47, source_count: 7  },
  { actor_a: "BIS",              actor_b: "Goldman Sachs",    topic: "Central Bank Independence",  intensity: 0.61, source_count: 9  },
];

export const mockSourceStances: SourceStance[] = [
  // Reuters
  { source: "Reuters",           source_type: "news",       topic: "Economy",    supportive: 12, critical:  4, neutral: 18, ambiguous: 3, total: 37, confidence: 0.71, document_count: 37, window_start: "2026-06-19", window_end: "2026-06-26" },
  { source: "Reuters",           source_type: "news",       topic: "Energy",     supportive:  5, critical:  9, neutral: 11, ambiguous: 2, total: 27, confidence: 0.68, document_count: 27, window_start: "2026-06-19", window_end: "2026-06-26" },
  // Bloomberg
  { source: "Bloomberg",         source_type: "news",       topic: "Economy",    supportive: 19, critical:  6, neutral:  8, ambiguous: 4, total: 37, confidence: 0.74, document_count: 37, window_start: "2026-06-19", window_end: "2026-06-26" },
  { source: "Bloomberg",         source_type: "news",       topic: "Technology", supportive: 21, critical:  3, neutral:  6, ambiguous: 2, total: 32, confidence: 0.79, document_count: 32, window_start: "2026-06-19", window_end: "2026-06-26" },
  // Financial Times
  { source: "Financial Times",   source_type: "news",       topic: "Economy",    supportive: 15, critical:  7, neutral: 12, ambiguous: 3, total: 37, confidence: 0.70, document_count: 37, window_start: "2026-06-19", window_end: "2026-06-26" },
  { source: "Financial Times",   source_type: "news",       topic: "Policy",     supportive:  7, critical: 11, neutral:  9, ambiguous: 4, total: 31, confidence: 0.67, document_count: 31, window_start: "2026-06-19", window_end: "2026-06-26" },
  // The Guardian
  { source: "The Guardian",      source_type: "news",       topic: "Policy",     supportive:  4, critical: 22, neutral:  7, ambiguous: 5, total: 38, confidence: 0.72, document_count: 38, window_start: "2026-06-19", window_end: "2026-06-26" },
  { source: "The Guardian",      source_type: "news",       topic: "Energy",     supportive:  3, critical: 19, neutral:  6, ambiguous: 4, total: 32, confidence: 0.75, document_count: 32, window_start: "2026-06-19", window_end: "2026-06-26" },
  // STAT News
  { source: "STAT News",         source_type: "news",       topic: "Health",     supportive: 22, critical:  2, neutral:  8, ambiguous: 3, total: 35, confidence: 0.77, document_count: 35, window_start: "2026-06-19", window_end: "2026-06-26" },
  // Wired
  { source: "Wired",             source_type: "news",       topic: "Technology", supportive: 17, critical:  5, neutral:  7, ambiguous: 3, total: 32, confidence: 0.73, document_count: 32, window_start: "2026-06-19", window_end: "2026-06-26" },
  // Blog
  { source: "energy-transition.blog", source_type: "blog", topic: "Energy",     supportive:  8, critical: 15, neutral:  3, ambiguous: 6, total: 32, confidence: 0.65, document_count: 32, window_start: "2026-06-19", window_end: "2026-06-26" },
  // Paper / journal
  { source: "Nature Climate Change", source_type: "paper", topic: "Energy",     supportive: 18, critical:  4, neutral:  6, ambiguous: 2, total: 30, confidence: 0.82, document_count: 30, window_start: "2026-06-19", window_end: "2026-06-26" },
  { source: "Nature Climate Change", source_type: "paper", topic: "Policy",     supportive: 14, critical:  6, neutral:  5, ambiguous: 3, total: 28, confidence: 0.81, document_count: 28, window_start: "2026-06-19", window_end: "2026-06-26" },
];

export const mockDriftEvents: StanceDriftEvent[] = [
  { source: "Reuters",         source_type: "news",  topic: "Economy",    from_stance: "neutral",    to_stance: "critical",   confidence_delta: 0.23, detected_at: "2026-06-19T03:00:00Z", window_pair: "2026-06-05:2026-06-12" },
  { source: "Reuters",         source_type: "news",  topic: "Economy",    from_stance: "critical",   to_stance: "supportive", confidence_delta: 0.31, detected_at: "2026-06-19T03:00:00Z", window_pair: "2026-06-12:2026-06-19" },
  { source: "Reuters",         source_type: "news",  topic: "Energy",     from_stance: "neutral",    to_stance: "critical",   confidence_delta: 0.25, detected_at: "2026-06-19T03:00:00Z", window_pair: "2026-06-05:2026-06-12" },
  { source: "Bloomberg",       source_type: "news",  topic: "Economy",    from_stance: "supportive", to_stance: "neutral",    confidence_delta: 0.22, detected_at: "2026-06-19T03:00:00Z", window_pair: "2026-06-05:2026-06-12" },
  { source: "Bloomberg",       source_type: "news",  topic: "Technology", from_stance: "neutral",    to_stance: "supportive", confidence_delta: 0.28, detected_at: "2026-06-19T03:00:00Z", window_pair: "2026-06-12:2026-06-19" },
  { source: "Financial Times", source_type: "news",  topic: "Policy",     from_stance: "neutral",    to_stance: "critical",   confidence_delta: 0.21, detected_at: "2026-06-19T03:00:00Z", window_pair: "2026-06-05:2026-06-12" },
  { source: "The Guardian",    source_type: "news",  topic: "Energy",     from_stance: "critical",   to_stance: "ambiguous",  confidence_delta: 0.19, detected_at: "2026-06-19T03:00:00Z", window_pair: "2026-06-12:2026-06-19" },
  { source: "energy-transition.blog", source_type: "blog", topic: "Energy", from_stance: "supportive", to_stance: "critical", confidence_delta: 0.34, detected_at: "2026-06-19T03:00:00Z", window_pair: "2026-06-05:2026-06-12" },
];

export const mockControversyGraph: ControversyGraph = {
  node_count: 10,
  edge_count: 12,
  nodes: [
    { id: "c1",  label: "Reuters",        source: "Reuters",        source_type: "news",       topic: "AI Regulation",        date: "2024-11-12", claim_text: "AI systems require mandatory pre-deployment safety audits before any commercial release.", confidence: 0.89, document_id: "doc-n-01" },
    { id: "c2",  label: "EU Commission",  source: "EU Commission",  source_type: "paper",      topic: "AI Regulation",        date: "2024-11-15", claim_text: "Current AI systems do not yet pose an existential safety risk requiring immediate regulatory intervention.", confidence: 0.76, document_id: "doc-p-01" },
    { id: "c3",  label: "Bloomberg",      source: "Bloomberg",      source_type: "news",       topic: "Interest Rate Policy", date: "2024-10-08", claim_text: "The Federal Reserve will cut rates by 50 basis points before year-end to stimulate economic growth.", confidence: 0.82, document_id: "doc-n-02" },
    { id: "c4",  label: "WSJ Editorial",  source: "WSJ Editorial",  source_type: "blog",       topic: "Interest Rate Policy", date: "2024-10-14", claim_text: "Premature rate cuts risk reigniting inflation that has not yet been durably subdued.", confidence: 0.71, document_id: "doc-b-01" },
    { id: "c5",  label: "OPEC Report",    source: "OPEC Report",    source_type: "paper",      topic: "Oil Production",       date: "2024-09-05", claim_text: "Global oil demand will peak no earlier than 2030, supporting continued upstream investment.", confidence: 0.93, document_id: "doc-p-02" },
    { id: "c6",  label: "IEA",            source: "IEA",            source_type: "paper",      topic: "Oil Production",       date: "2024-09-10", claim_text: "Oil demand is set to peak well before 2030 as EV adoption and renewable capacity accelerate.", confidence: 0.88, document_id: "doc-p-03" },
    { id: "c7",  label: "FT",             source: "FT",             source_type: "news",       topic: "Trade Tariffs",        date: "2024-08-22", claim_text: "Broad tariffs on Chinese imports will reduce the US trade deficit and protect domestic manufacturing.", confidence: 0.67, document_id: "doc-n-03" },
    { id: "c8",  label: "IMF Report",     source: "IMF Report",     source_type: "paper",      topic: "Trade Tariffs",        date: "2024-08-28", claim_text: "Tariff escalation will reduce global GDP by 0.7% and primarily harm the economies imposing them.", confidence: 0.91, document_id: "doc-p-04" },
    { id: "c9",  label: "Nature Podcast", source: "Nature Podcast", source_type: "transcript", topic: "Drug Pricing",         date: "2024-07-11", claim_text: "Price controls on pharmaceuticals will stifle R&D investment and reduce long-term innovation.", confidence: 0.74, document_id: "doc-t-01" },
    { id: "c10", label: "WHO",            source: "WHO",            source_type: "paper",      topic: "Drug Pricing",         date: "2024-07-20", claim_text: "Excessive drug pricing by pharmaceutical companies is the primary barrier to equitable global healthcare access.", confidence: 0.85, document_id: "doc-p-05" },
  ],
  edges: [
    { source: "c1",  target: "c2",  severity: 0.87, relation: "contradicts", conflict_type: "direct"  },
    { source: "c2",  target: "c1",  severity: 0.82, relation: "contradicts", conflict_type: "direct"  },
    { source: "c3",  target: "c4",  severity: 0.78, relation: "contradicts", conflict_type: "direct"  },
    { source: "c4",  target: "c3",  severity: 0.74, relation: "contradicts", conflict_type: "direct"  },
    { source: "c5",  target: "c6",  severity: 0.93, relation: "contradicts", conflict_type: "direct"  },
    { source: "c6",  target: "c5",  severity: 0.91, relation: "contradicts", conflict_type: "direct"  },
    { source: "c7",  target: "c8",  severity: 0.83, relation: "contradicts", conflict_type: "direct"  },
    { source: "c8",  target: "c7",  severity: 0.86, relation: "contradicts", conflict_type: "direct"  },
    { source: "c9",  target: "c10", severity: 0.72, relation: "contradicts", conflict_type: "implied" },
    { source: "c10", target: "c9",  severity: 0.79, relation: "contradicts", conflict_type: "implied" },
    { source: "c1",  target: "c7",  severity: 0.55, relation: "contradicts", conflict_type: "implied" },
    { source: "c3",  target: "c5",  severity: 0.48, relation: "contradicts", conflict_type: "implied" },
  ],
};

export const mockFramesBySource: FrameSource[] = [
  { source: "Reuters",                source_type: "news",       frames: { economic: 0.61, political: 0.42, security: 0.30, scientific: 0.16, humanitarian: 0.18, legal: 0.19, other: 0.05 }, doc_count: 64, dominant: "economic",     concentrated: true,  concentrated_frame: "economic"    },
  { source: "Bloomberg",              source_type: "news",       frames: { economic: 0.72, political: 0.35, security: 0.24, scientific: 0.20, humanitarian: 0.12, legal: 0.15, other: 0.04 }, doc_count: 69, dominant: "economic",     concentrated: true,  concentrated_frame: "economic"    },
  { source: "Financial Times",        source_type: "news",       frames: { economic: 0.58, political: 0.47, security: 0.26, scientific: 0.18, humanitarian: 0.21, legal: 0.23, other: 0.06 }, doc_count: 68, dominant: "economic",     concentrated: false, concentrated_frame: null          },
  { source: "The Guardian",           source_type: "news",       frames: { economic: 0.34, political: 0.52, security: 0.28, scientific: 0.22, humanitarian: 0.46, legal: 0.24, other: 0.08 }, doc_count: 70, dominant: "political",    concentrated: false, concentrated_frame: null          },
  { source: "STAT News",              source_type: "news",       frames: { economic: 0.22, political: 0.18, security: 0.12, scientific: 0.44, humanitarian: 0.38, legal: 0.14, other: 0.06 }, doc_count: 35, dominant: "scientific",   concentrated: false, concentrated_frame: null          },
  { source: "Wired",                  source_type: "news",       frames: { economic: 0.30, political: 0.22, security: 0.18, scientific: 0.55, humanitarian: 0.14, legal: 0.12, other: 0.10 }, doc_count: 32, dominant: "scientific",   concentrated: false, concentrated_frame: null          },
  { source: "energy-transition.blog", source_type: "blog",       frames: { economic: 0.28, political: 0.36, security: 0.14, scientific: 0.42, humanitarian: 0.30, legal: 0.10, other: 0.18 }, doc_count: 32, dominant: "scientific",   concentrated: false, concentrated_frame: null          },
  { source: "Nature Climate Change",  source_type: "paper",      frames: { economic: 0.14, political: 0.10, security: 0.08, scientific: 0.78, humanitarian: 0.22, legal: 0.11, other: 0.04 }, doc_count: 58, dominant: "scientific",   concentrated: true,  concentrated_frame: "scientific"  },
  { source: "IMF Working Papers",     source_type: "paper",      frames: { economic: 0.82, political: 0.28, security: 0.10, scientific: 0.34, humanitarian: 0.16, legal: 0.20, other: 0.03 }, doc_count: 24, dominant: "economic",     concentrated: true,  concentrated_frame: "economic"    },
  { source: "Energy Policy Podcast",  source_type: "transcript", frames: { economic: 0.44, political: 0.58, security: 0.22, scientific: 0.28, humanitarian: 0.18, legal: 0.24, other: 0.09 }, doc_count: 18, dominant: "political",    concentrated: false, concentrated_frame: null          },
];

// Outlet editorial-framing clusters (#115) — 5 clusters, PCA 2D projection
// Cluster labels derived from dominant frame centroid; pca_x/y are illustrative
// but maintain realistic relative distances between editorial families.
export const mockOutletClusters: OutletCluster[] = [
  // Cluster 0 — economic-dominant (financial press)
  { source: "Bloomberg",              source_type: "news",       cluster_id: 0, cluster_label: "economic-dominant",         pca_x: -1.42, pca_y:  0.28, dominant_frame: "economic",     doc_count: 69, computed_at: null },
  { source: "Reuters",                source_type: "news",       cluster_id: 0, cluster_label: "economic-dominant",         pca_x: -1.28, pca_y:  0.11, dominant_frame: "economic",     doc_count: 64, computed_at: null },
  { source: "Financial Times",        source_type: "news",       cluster_id: 0, cluster_label: "economic-dominant",         pca_x: -1.10, pca_y:  0.47, dominant_frame: "economic",     doc_count: 68, computed_at: null },
  { source: "IMF Working Papers",     source_type: "paper",      cluster_id: 0, cluster_label: "economic-dominant",         pca_x: -1.55, pca_y: -0.18, dominant_frame: "economic",     doc_count: 24, computed_at: null },
  // Cluster 1 — scientific-dominant (science & research)
  { source: "Nature Climate Change",  source_type: "paper",      cluster_id: 1, cluster_label: "scientific-dominant",       pca_x:  1.60, pca_y:  0.95, dominant_frame: "scientific",   doc_count: 58, computed_at: null },
  { source: "STAT News",              source_type: "news",       cluster_id: 1, cluster_label: "scientific-dominant",       pca_x:  1.22, pca_y:  0.82, dominant_frame: "scientific",   doc_count: 35, computed_at: null },
  { source: "Wired",                  source_type: "news",       cluster_id: 1, cluster_label: "scientific-dominant",       pca_x:  1.38, pca_y:  0.60, dominant_frame: "scientific",   doc_count: 32, computed_at: null },
  // Cluster 2 — balanced-political-economic (broad broadsheets)
  { source: "The Guardian",           source_type: "news",       cluster_id: 2, cluster_label: "balanced-political-economic", pca_x: -0.22, pca_y:  1.18, dominant_frame: "political",   doc_count: 70, computed_at: null },
  { source: "energy-transition.blog", source_type: "blog",       cluster_id: 2, cluster_label: "balanced-political-economic", pca_x:  0.18, pca_y:  1.32, dominant_frame: "political",   doc_count: 32, computed_at: null },
  // Cluster 3 — political-focused (policy commentary)
  { source: "Energy Policy Podcast",  source_type: "transcript", cluster_id: 3, cluster_label: "political-focused",         pca_x: -0.60, pca_y: -1.10, dominant_frame: "political",   doc_count: 18, computed_at: null },
];

// Outlet transparency rankings (#116) — three-dimension scores + sparkline trends
// trend arrays simulate 6 weekly snapshots, oldest → newest
export const mockOutletRanking: OutletScore[] = [
  { rank: 1, source: "Nature Climate Change",  source_type: "paper",      score_date: "2025-06-23", frame_diversity: 0.74, attribution_rate: 0.88, stance_neutrality: 0.81, composite_score: 0.81, doc_count: 58, claim_count: 42, trend: [0.67, 0.70, 0.72, 0.75, 0.79, 0.81] },
  { rank: 2, source: "Reuters",                source_type: "news",       score_date: "2025-06-23", frame_diversity: 0.71, attribution_rate: 0.82, stance_neutrality: 0.78, composite_score: 0.77, doc_count: 64, claim_count: 78, trend: [0.60, 0.65, 0.70, 0.73, 0.75, 0.77] },
  { rank: 3, source: "Financial Times",        source_type: "news",       score_date: "2025-06-23", frame_diversity: 0.68, attribution_rate: 0.79, stance_neutrality: 0.72, composite_score: 0.73, doc_count: 68, claim_count: 83, trend: [0.55, 0.60, 0.65, 0.68, 0.70, 0.73] },
  { rank: 4, source: "Bloomberg",              source_type: "news",       score_date: "2025-06-23", frame_diversity: 0.55, attribution_rate: 0.84, stance_neutrality: 0.74, composite_score: 0.71, doc_count: 69, claim_count: 91, trend: [0.58, 0.61, 0.64, 0.67, 0.69, 0.71] },
  { rank: 5, source: "STAT News",              source_type: "news",       score_date: "2025-06-23", frame_diversity: 0.62, attribution_rate: 0.75, stance_neutrality: 0.66, composite_score: 0.68, doc_count: 35, claim_count: 31, trend: [0.56, 0.58, 0.60, 0.63, 0.66, 0.68] },
  { rank: 6, source: "The Guardian",           source_type: "news",       score_date: "2025-06-23", frame_diversity: 0.70, attribution_rate: 0.58, stance_neutrality: 0.64, composite_score: 0.64, doc_count: 70, claim_count: 55, trend: [0.62, 0.63, 0.63, 0.64, 0.64, 0.64] },
  { rank: 7, source: "Wired",                  source_type: "news",       score_date: "2025-06-23", frame_diversity: 0.58, attribution_rate: 0.61, stance_neutrality: 0.68, composite_score: 0.62, doc_count: 32, claim_count: 24, trend: [0.50, 0.53, 0.56, 0.58, 0.60, 0.62] },
  { rank: 8, source: "energy-transition.blog", source_type: "blog",       score_date: "2025-06-23", frame_diversity: 0.64, attribution_rate: 0.42, stance_neutrality: 0.55, composite_score: 0.54, doc_count: 32, claim_count: 18, trend: [0.48, 0.49, 0.51, 0.52, 0.53, 0.54] },
  { rank: 9, source: "IMF Working Papers",     source_type: "paper",      score_date: "2025-06-23", frame_diversity: 0.38, attribution_rate: 0.91, stance_neutrality: 0.60, composite_score: 0.63, doc_count: 24, claim_count: 44, trend: [0.55, 0.57, 0.59, 0.61, 0.62, 0.63] },
  { rank:10, source: "Energy Policy Podcast",  source_type: "transcript", score_date: "2025-06-23", frame_diversity: 0.61, attribution_rate: 0.34, stance_neutrality: 0.52, composite_score: 0.49, doc_count: 18, claim_count: 12, trend: [0.44, 0.45, 0.46, 0.47, 0.48, 0.49] },
];

