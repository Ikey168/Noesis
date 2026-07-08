"""
The investigation engine - Noesis's first-class case-work subsystem.

Where the OSINT plane provides the investigative *primitives* (corroboration,
contradiction scan, dossiers, timelines, provenance), this package provides
the *engine* that drives them toward an answer. A case is a durable object:

* a **question** under investigation,
* competing **hypotheses** (never fewer than two - an investigation that
  cannot state its own null hypothesis is advocacy, not analysis),
* **evidence** rows harvested by pursuing leads, each cited to a source and
  weighted by that source's credibility,
* **leads** - the planned, replayable tool calls that produced the evidence,
* an append-only **journal** so every case is reconstructable step by step.

The engine plans leads from the case scope, pursues them through the OSINT
composition layer, scores the hypothesis matrix ACH-style (weighted support
and contradiction per independent source, diagnostic evidence called out),
and only ever concludes through the evidence-discipline gate: enough
independent sources, a real margin over the runner-up, no unanswered
contradiction, no open leads. When the gate fails the case stays open and the
engine names the gaps instead of manufacturing a verdict.

Stdlib-only; the DuckDB connection is injected by the caller (the API's
single writer, an MCP tool holding the write lock, or a test).
"""

from src.investigation.engine import (
    CONCLUDE_MIN_INDEPENDENT_SOURCES,
    CONCLUDE_MIN_WEIGHTED_MARGIN,
    advance_case,
    case_file,
    conclude_case,
    hypothesis_matrix,
    open_case,
    plan_leads,
    pursue_lead,
    pursue_open_leads,
    run_case,
)
from src.investigation.report import case_brief, render_markdown
from src.investigation.store import ensure_schema, list_cases

__all__ = [
    "open_case",
    "plan_leads",
    "pursue_lead",
    "pursue_open_leads",
    "advance_case",
    "hypothesis_matrix",
    "conclude_case",
    "run_case",
    "case_file",
    "case_brief",
    "render_markdown",
    "list_cases",
    "ensure_schema",
    "CONCLUDE_MIN_INDEPENDENT_SOURCES",
    "CONCLUDE_MIN_WEIGHTED_MARGIN",
]
