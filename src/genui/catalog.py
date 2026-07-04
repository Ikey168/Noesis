"""
Panel catalog — the single source of truth for what the generative UI can
render.

Every entry maps a panel type to the backend endpoint that feeds it, the
warehouse tables it needs, the domain-pack ``ui_flag`` that gates it, the
intent facets it serves, and layout defaults. The frontend keeps a renderer
registry keyed by the same ``type`` strings (apps/web/src/genui/registry.tsx);
the two must stay in sync — the smoke test asserts the catalog is exposed via
``GET /api/v1/ui/panels`` so the frontend can introspect it.

Stdlib-only on purpose: importing this module must never fail, otherwise the
route registration in src/api/app.py silently disables the endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class PanelDef:
    """Static definition of one renderable panel type."""

    type: str
    title: str
    description: str
    endpoint: Optional[str]
    facets: Tuple[str, ...]
    tables: Tuple[str, ...] = ()
    ui_flag: Optional[str] = None
    default_span: int = 6
    topic_param: Optional[str] = None
    source_type_param: Optional[str] = None
    days_param: Optional[str] = None
    # Upper bound of the endpoint's `days` Query validator, so generated
    # params never draw a 422 from the endpoint they target.
    max_days: Optional[int] = None


# Facets an intent can express. The planner scores each facet from keyword
# evidence and selects panels serving the highest-scoring facets.
FACETS: Tuple[str, ...] = (
    "overview",
    "trend",
    "sentiment",
    "claims",
    "stance",
    "actors",
    "conflict",
    "sources",
    "entities",
    "events",
    "library",
)

PANEL_CATALOG: Tuple[PanelDef, ...] = (
    PanelDef(
        type="note",
        title="Plan",
        description="Narrative note explaining how the canvas was assembled.",
        endpoint=None,
        facets=FACETS,
        default_span=12,
    ),
    # Overview panels anchor availability on the "documents" corpus (Track
    # N2 / R3): adaptivity treats it as the union of the documents table and
    # news_articles, so a zero-news corpus still gets a live overview.
    PanelDef(
        type="kpi_row",
        title="Signal summary",
        description="Headline counts across articles, clusters and topics.",
        endpoint="/api/v1/news/articles",
        facets=("overview",),
        tables=("documents",),
        default_span=12,
    ),
    PanelDef(
        type="articles",
        title="Latest documents",
        description="Most recent matching articles and documents.",
        endpoint="/api/v1/news/articles",
        facets=("overview", "sentiment"),
        tables=("documents",),
        default_span=6,
    ),
    PanelDef(
        type="documents",
        title="Library",
        description="Ingested documents across all source types (books, papers, transcripts, …).",
        endpoint="/api/v1/documents",
        facets=("library", "overview"),
        default_span=6,
        source_type_param="source_type",
    ),
    PanelDef(
        type="anomaly_timeline",
        title="Anomaly timeline",
        description="Coverage-volume and sentiment windows flagged as unusual, with expected bands and robust z-scores.",
        endpoint=None,
        facets=("trend", "overview"),
        tables=("news_articles",),
        default_span=6,
        topic_param="topic",
    ),
    PanelDef(
        type="trending",
        title="Trending topics",
        description="Topics ranked by mention velocity.",
        endpoint="/topics/trending",
        facets=("overview", "trend", "events"),
        tables=("news_articles",),
        ui_flag="trending",
        default_span=6,
        days_param="days",
        max_days=30,
    ),
    PanelDef(
        type="clusters",
        title="Event clusters",
        description="Grouped event coverage with velocity and impact.",
        endpoint="/api/v1/events/clusters",
        facets=("overview", "events"),
        tables=("news_articles",),
        ui_flag="clusters",
        default_span=6,
    ),
    PanelDef(
        type="event_axis",
        title="Coverage timeline",
        description="A topic's notable coverage moments on a dated axis, each marker sized by coverage volume and colored by sentiment; relative timing and clustering are read directly, not inferred from a list.",
        endpoint=None,
        facets=("events", "trend"),
        tables=("news_articles",),
        default_span=6,
        topic_param="topic",
        days_param="days",
        max_days=90,
    ),
    PanelDef(
        type="watchlists",
        title="Watchlist",
        description="Tracked entities and topics with mention velocity and alerts.",
        endpoint=None,
        facets=("events", "trend"),
        ui_flag="watchlists",
        default_span=6,
    ),
    PanelDef(
        type="timeline",
        title="Story timeline",
        description="Chronological development of a tracked story.",
        endpoint=None,
        facets=("events",),
        ui_flag="timeline",
        default_span=6,
        topic_param="topic",
    ),
    PanelDef(
        type="sentiment_heatmap",
        title="Sentiment heatmap",
        description="Topic × time sentiment intensity grid.",
        endpoint="/news_sentiment/heatmap",
        facets=("sentiment", "trend"),
        tables=("news_articles",),
        ui_flag="sentiment_dashboard",
        default_span=6,
        days_param="days",
        max_days=60,
    ),
    PanelDef(
        type="topic_sentiment",
        title="Sentiment by topic",
        description="Average sentiment score per topic.",
        endpoint="/news_sentiment/topics",
        facets=("sentiment",),
        tables=("news_articles",),
        ui_flag="sentiment_dashboard",
        default_span=6,
        days_param="days",
        max_days=90,
    ),
    PanelDef(
        type="sentiment_trend",
        title="Sentiment trajectory",
        description="How sentiment on a topic moves over time, as a smoothed line with a confidence band; never a bare point estimate.",
        endpoint=None,
        facets=("sentiment", "trend"),
        tables=("news_articles",),
        default_span=6,
        topic_param="topic",
        days_param="days",
        max_days=90,
    ),
    PanelDef(
        type="entity_graph",
        title="Entity graph",
        description="Co-mention network of entities in recent coverage.",
        endpoint="/api/v1/entity_graph",
        facets=("entities", "actors", "overview"),
        tables=("documents",),
        ui_flag="influence_graph",
        default_span=6,
        days_param="days",
        max_days=30,
    ),
    PanelDef(
        type="claims",
        title="Extracted claims",
        description="Claims mined from documents with fact-check verdicts.",
        endpoint="/api/v1/arguments/claims",
        facets=("claims", "conflict"),
        tables=("argument_claims",),
        default_span=6,
        topic_param="topic",
        source_type_param="source_type",
    ),
    PanelDef(
        type="claim_verdicts",
        title="Fact-check scoreboard",
        description="Verified, disputed and unverified claim counts per source, as grouped bars; which outlets carry corroborated claims and which carry unchecked ones is read directly.",
        endpoint=None,
        facets=("claims", "sources"),
        tables=("argument_claims",),
        default_span=6,
        topic_param="topic",
        source_type_param="source_type",
    ),
    PanelDef(
        type="stance",
        title="Stance breakdown",
        description="Supportive / critical / neutral stance mix per topic.",
        endpoint="/api/v1/arguments/stance",
        facets=("stance", "conflict", "sentiment"),
        tables=("source_stances",),
        default_span=6,
        topic_param="topic",
        source_type_param="source_type",
    ),
    PanelDef(
        type="frames",
        title="Framing by source",
        description="How each outlet frames the story (economic, legal, …).",
        endpoint="/api/v1/arguments/frames/source",
        facets=("sources", "claims"),
        tables=("document_frames",),
        default_span=6,
        topic_param="topic",
        source_type_param="source_type",
    ),
    PanelDef(
        type="positions",
        title="Actor positions",
        description="Policy positions held by actors, with updates over time.",
        endpoint="/api/v1/arguments/positions",
        facets=("actors", "stance"),
        tables=("policy_positions",),
        default_span=6,
        topic_param="topic",
        source_type_param="source_type",
    ),
    PanelDef(
        type="controversy",
        title="Conflicts",
        description="Actor pairs with contradicting claims, by intensity.",
        endpoint="/api/v1/arguments/controversy",
        facets=("conflict", "claims"),
        tables=("claim_conflicts",),
        default_span=6,
        topic_param="topic",
        source_type_param="source_type",
    ),
    PanelDef(
        type="drift",
        title="Stance drift",
        description="Detected stance reversals and shifts per source.",
        endpoint="/api/v1/arguments/stance/drift",
        facets=("trend", "stance"),
        tables=("stance_drift_events",),
        default_span=6,
        topic_param="topic",
        source_type_param="source_type",
    ),
    PanelDef(
        type="outlet_ranking",
        title="Outlet transparency ranking",
        description="Outlets scored by framing diversity, attribution, neutrality.",
        endpoint="/api/v1/arguments/outlets/ranking",
        facets=("sources",),
        tables=("outlet_scores",),
        default_span=6,
        source_type_param="source_type",
    ),
    PanelDef(
        type="outlet_clusters",
        title="Outlet clusters",
        description="Outlets grouped by editorial framing (PCA scatter).",
        endpoint="/api/v1/arguments/outlets/clusters",
        facets=("sources", "entities"),
        tables=("outlet_clusters",),
        default_span=6,
        source_type_param="source_type",
    ),
    PanelDef(
        type="coverage_flow",
        title="Coverage flow",
        description="How the corpus composes as a flow from source type to category to sentiment; ribbon width is the share of coverage, so routing and proportion are read at a glance rather than from a table.",
        endpoint=None,
        facets=("sources", "overview"),
        tables=("news_articles",),
        default_span=6,
        days_param="days",
        max_days=90,
    ),
    PanelDef(
        type="actors",
        title="Key actors",
        description="Most-mentioned speakers, subjects and authors.",
        endpoint="/api/v1/arguments/actors/summary",
        facets=("actors", "entities"),
        tables=("document_actors",),
        default_span=6,
        source_type_param="source_type",
    ),
    # Analytics-breadth panels (R6 / Track DS Wave 1b).
    PanelDef(
        type="lead_lag",
        title="Who leads, who follows",
        description="Outlets ranked by whether they set the agenda or follow it, from cross-correlation lead-lag of coverage.",
        endpoint=None,
        facets=("sources", "trend"),
        tables=("news_articles",),
        default_span=6,
        topic_param="topic",
    ),
    PanelDef(
        type="narrative_thread",
        title="Narrative threads",
        description="Competing storylines on a topic, clustered from document text with size and cohesion.",
        endpoint=None,
        facets=("events", "overview"),
        tables=("news_articles",),
        default_span=6,
        topic_param="topic",
        days_param="days",
        max_days=90,
    ),
    PanelDef(
        type="drift_trajectory",
        title="Meaning drift",
        description="How a term's coverage context shifts over time, with rising and falling associated terms.",
        endpoint=None,
        facets=("trend",),
        tables=("news_articles",),
        default_span=6,
        topic_param="term",
    ),
    PanelDef(
        type="forecast",
        title="Coverage forecast",
        description="Projected coverage velocity for a topic with a prediction band (never a bare point forecast).",
        endpoint=None,
        facets=("trend",),
        tables=("news_articles",),
        default_span=6,
        topic_param="topic",
    ),
    # Research-pack panels (R7 / Track N1), gated by the research ui_flags.
    PanelDef(
        type="venues",
        title="Venue credibility",
        description="Publication venues scored by concept diversity, attribution and citation impact, generalizing the outlet transparency ranking.",
        endpoint=None,
        facets=("sources", "library"),
        tables=("documents",),
        ui_flag="venues",
        default_span=6,
    ),
    PanelDef(
        type="citation_graph",
        title="Citation graph",
        description="The paper citation network: papers linked by their references, sized by citation count.",
        endpoint=None,
        facets=("entities", "library"),
        tables=("documents",),
        ui_flag="citation_graph",
        default_span=6,
        topic_param="topic",
    ),
    PanelDef(
        type="literature_claims",
        title="Literature claims",
        description="Claims mined from papers with fact-check verdicts and attribution, from the shared claim layer.",
        endpoint=None,
        facets=("claims", "library"),
        tables=("argument_claims",),
        ui_flag="literature_claims",
        default_span=6,
        topic_param="topic",
    ),
    # Provisioning plane (R8 / Track P): agent-deployed namespaced KGs surface
    # as a scoped panel via R2 discovery from the provisioning server.
    PanelDef(
        type="provisioned_kg",
        title="Provisioned knowledge graphs",
        description="Agent-deployed namespaced knowledge graphs: the scoped documents, entities and claims per namespace, plus the sources feeding each and why they were selected.",
        endpoint=None,
        facets=("entities", "overview", "library"),
        tables=("provisioned_kgs",),
        default_span=6,
        topic_param="kg",
    ),
    # OSINT composition (R10 / Track OSINT), gated by the `osint` ui_flag;
    # pure-composition reads over the existing claim / evidence / conflict
    # layers.
    PanelDef(
        type="corroboration",
        title="Claim corroboration",
        description="Independent sources supporting or contradicting a claim, weighted by source credibility; single-sourced claims are flagged, never given a false confidence.",
        endpoint=None,
        facets=("claims", "sources", "conflict"),
        tables=("argument_claims",),
        ui_flag="osint",
        default_span=6,
    ),
    PanelDef(
        type="reliability_card",
        title="Source reliability",
        description="OSINT source vetting: the outlet transparency score generalized to any source type, with corroboration hit-rate and correction history.",
        endpoint=None,
        facets=("sources",),
        tables=("outlet_scores",),
        ui_flag="osint",
        default_span=6,
    ),
    PanelDef(
        type="contradiction_ledger",
        title="Contradiction ledger",
        description="Where the public record disagrees with itself: contradicting claim pairs with both sources and citations; uncited entries are flagged, never hidden.",
        endpoint=None,
        facets=("conflict", "claims"),
        tables=("claim_conflicts",),
        ui_flag="osint",
        default_span=6,
        topic_param="topic",
    ),
    # OSINT investigation surface (R11 / Track OSINT phase 2), gated by osint.
    PanelDef(
        type="entity_dossier",
        title="Entity dossier",
        description="A cited brief for an entity from ingested public documents: every mention, aliases, first and last seen, and connected entities, each line linked to its source. Person entities require a document; no inference-only facts.",
        endpoint=None,
        facets=("entities", "actors"),
        tables=("document_actors",),
        ui_flag="osint",
        default_span=6,
        topic_param="entity",
    ),
    PanelDef(
        type="relationship_path",
        title="Connection path",
        description="How two entities are connected across the corpus, via the shortest co-mention path; each edge carries the cited documents that establish it. Resolution ambiguity is surfaced, not collapsed.",
        endpoint=None,
        facets=("entities", "actors"),
        tables=("document_actors",),
        ui_flag="osint",
        default_span=6,
    ),
    PanelDef(
        type="evidence_timeline",
        title="Evidence timeline",
        description="A reconstructed event sequence from dated, cited claims, each event carrying its corroboration density (independent-source count); uncited entries flagged.",
        endpoint=None,
        facets=("events", "trend", "claims"),
        tables=("argument_claims",),
        ui_flag="osint",
        default_span=6,
        topic_param="topic",
    ),
    PanelDef(
        type="provenance_trace",
        title="Provenance trace",
        description="The full chain behind an artifact: from the source that ingested it, through the document and its enrichments, to the claim and any provisioned KG it was routed into, every stage cited.",
        endpoint=None,
        facets=("claims", "sources", "library"),
        tables=("argument_claims",),
        ui_flag="osint",
        default_span=6,
        topic_param="claim_id",
    ),
)

PANEL_TYPES: Tuple[str, ...] = tuple(p.type for p in PANEL_CATALOG)

_BY_TYPE: Dict[str, PanelDef] = {p.type: p for p in PANEL_CATALOG}

# Runtime panel registry (M9.3): installed domain packs contribute panel defs
# here at install time, so a pack surfaces its panels without editing the static
# catalog. A static entry is authoritative and is never overridden by a pack.
_RUNTIME_PANELS: Dict[str, PanelDef] = {}


def register_panel(paneldef: PanelDef) -> None:
    """Register a runtime panel def (e.g. from an installed domain pack). A panel
    whose type is already in the static catalog is left untouched."""
    if paneldef.type not in _BY_TYPE:
        _RUNTIME_PANELS[paneldef.type] = paneldef


def unregister_panel(panel_type: str) -> None:
    """Remove a runtime panel def (pack uninstall)."""
    _RUNTIME_PANELS.pop(panel_type, None)


def runtime_panels() -> Tuple[PanelDef, ...]:
    """The currently-registered runtime (pack) panel defs."""
    return tuple(_RUNTIME_PANELS.values())


def all_panel_types() -> Tuple[str, ...]:
    """Every renderable panel type: static catalog plus installed-pack panels."""
    return PANEL_TYPES + tuple(t for t in _RUNTIME_PANELS if t not in _BY_TYPE)


def get_panel_def(panel_type: str) -> Optional[PanelDef]:
    """Return the catalog entry for a panel type, or None. Consults the static
    catalog first, then installed-pack panels."""
    return _BY_TYPE.get(panel_type) or _RUNTIME_PANELS.get(panel_type)


def panel_catalog_dict() -> List[Dict[str, Any]]:
    """JSON-serializable catalog for the /api/v1/ui/panels endpoint."""
    return [
        {
            "type": p.type,
            "title": p.title,
            "description": p.description,
            "endpoint": p.endpoint,
            "facets": list(p.facets),
            "tables": list(p.tables),
            "ui_flag": p.ui_flag,
            "default_span": p.default_span,
            "topic_param": p.topic_param,
            "source_type_param": p.source_type_param,
            "days_param": p.days_param,
            "max_days": p.max_days,
        }
        for p in PANEL_CATALOG
    ]
