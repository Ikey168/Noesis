"""
The daily brief: a thin consumer of the KB contract (#960).

One reader, one page: per-domain "what changed" computed from
``kb_diff``/``kb_documents`` — never from raw feeds — under a hard length
budget with an explicit dropped count (a brief that grows unboundedly
recreates the overload it exists to solve). Research domains (``papers``
tags) get a dedicated **New publications** section listing each new paper,
cited, grouped by feed.

Every line is cited (source + url), analytic entries carry
``prediction_mode``/confidence, and the header states the corpus's
evidence quality — the honesty rules of the contract carried into prose.

This module deliberately touches nothing below the contract: it proves the
layering (#971) and doubles as the template for the next consumer.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from src.kb import contract

#: total item budget across all domains (per #960's overload discipline)
DEFAULT_BUDGET = 15
#: publications listed per research domain before "…and N more"
MAX_PUBLICATIONS = 10


def _default_since() -> str:
    return (
        datetime.now(timezone.utc) - timedelta(hours=24)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_research_domain(definition) -> bool:
    return "papers" in definition.tags or "research" in definition.tags


def _cluster_line(cluster: Dict[str, Any], note: str = "") -> Dict[str, Any]:
    representative = cluster["representative"]
    sources = sorted(
        {
            str(citation.get("source") or "").strip()
            for citation in cluster["citations"]
            if citation.get("source")
        }
    )
    return {
        "text": (representative.get("claim_text") or "").strip(),
        "url": representative.get("url"),
        "sources": sources,
        "corroboration": cluster.get("corroboration", 1),
        "note": note,
        "contradicted": bool(cluster.get("contradictions")),
        "recency": cluster.get("last_ingested_ms", 0),
    }


def generate_brief(
    domains: Optional[List[str]] = None,
    since: Optional[str] = None,
    budget: int = DEFAULT_BUDGET,
    conn=None,
    config_path=None,
) -> Dict[str, Any]:
    """Build the brief; returns ``{markdown, sections, meta}``.

    ``since`` defaults to 24h ago (UTC). Items across all non-research
    sections are ranked (corroboration, then recency) and cut to
    ``budget``, with the dropped count reported — never silently.
    """
    from src.kb.registry import load_registry

    registry = load_registry(config_path)
    names = domains or registry.names()
    since = since or _default_since()

    sections: List[Dict[str, Any]] = []
    candidates: List[Dict[str, Any]] = []  # rankable items across domains

    for name in names:
        definition = registry.get(name)
        diff = contract.kb_diff(name, since, conn=conn, config_path=config_path)["data"]

        section: Dict[str, Any] = {
            "domain": name,
            "research": _is_research_domain(definition),
            "documents_new": diff["documents"]["new"],
            "documents_total": diff["documents"]["total"],
            "sources_delivered": diff["documents"]["sources_delivered"],
            "items": [],
            "contradictions": diff["new_contradictions"],
            "entity_surges": diff["entity_surges"] or [],
            "publications": [],
        }

        if section["research"]:
            docs = contract.kb_documents(
                name, since=since, limit=200, conn=conn, config_path=config_path
            )["data"]
            section["publications"] = [
                {
                    "title": (doc.get("title") or "(untitled)").strip(),
                    "source": doc.get("source_id") or "",
                    "url": doc.get("url"),
                }
                for doc in docs
            ]
        else:
            for cluster in diff["new_clusters"]:
                item = _cluster_line(cluster)
                if item["text"]:
                    candidates.append({**item, "domain": name})
            for cluster in diff["gained_corroboration"]:
                item = _cluster_line(
                    cluster,
                    note="gained sources: " + ", ".join(cluster.get("new_sources", [])),
                )
                if item["text"]:
                    candidates.append({**item, "domain": name})

        sections.append(section)

    # ── the budget: rank, cut, and say what was dropped ──────────────────
    candidates.sort(key=lambda c: (c["corroboration"], c["recency"]), reverse=True)
    kept, dropped = candidates[: int(budget)], max(0, len(candidates) - int(budget))
    for item in kept:
        for section in sections:
            if section["domain"] == item["domain"]:
                section["items"].append(item)
                break

    # ── evidence-quality header line (via any domain's coverage) ─────────
    quality_line = None
    if names:
        coverage = contract.kb_coverage(
            names[0], conn=conn, config_path=config_path
        )["data"]
        quality = coverage.get("evidence_quality")
        if quality and quality.get("total_rows"):
            fraction = quality["model_grade_fraction"]
            quality_line = (
                f"{round(fraction * 100)}% of underlying analysis is model-grade"
                f" ({quality['total_rows']} prediction rows)"
            )

    meta = {
        "since": since,
        "generated_at_ms": int(time.time() * 1000),
        "budget": int(budget),
        "kept": len(kept),
        "dropped": dropped,
        "evidence_quality": quality_line,
    }
    return {
        "markdown": _render_markdown(sections, meta),
        "sections": sections,
        "meta": meta,
    }


def _render_markdown(sections: List[Dict[str, Any]], meta: Dict[str, Any]) -> str:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [f"# Noesis daily brief — {day}", ""]
    subtitle = [
        f"since {meta['since']}",
        f"{meta['kept']} items",
    ]
    if meta["dropped"]:
        subtitle.append(f"{meta['dropped']} below the budget line (not shown)")
    if meta["evidence_quality"]:
        subtitle.append(meta["evidence_quality"])
    lines += ["_" + " · ".join(subtitle) + "_", ""]

    for section in sections:
        has_content = (
            section["items"]
            or section["publications"]
            or section["contradictions"]
            or section["entity_surges"]
        )
        header = (
            f"## {section['domain']}"
            f" — {section['documents_new']} new of"
            f" {section['documents_total']} documents"
        )
        lines.append(header)
        if not has_content:
            note = (
                "nothing new (no arrivals from any feed)"
                if not section["sources_delivered"]
                else "arrivals, but nothing cleared the bar"
            )
            lines += [f"_{note}_", ""]
            continue

        for item in section["items"]:
            cite = ", ".join(item["sources"]) or "uncited — flagged"
            line = f"- **{item['text']}**  \n  {item['corroboration']}× corroborated ({cite})"
            if item["url"]:
                line += f" — [link]({item['url']})"
            if item["note"]:
                line += f" — _{item['note']}_"
            if item["contradicted"]:
                line += " — ⚡ contested"
            lines.append(line)

        if section["publications"]:
            shown = section["publications"][:MAX_PUBLICATIONS]
            lines.append(f"\n### New publications ({len(section['publications'])})")
            for publication in shown:
                line = f"- {publication['title']} — _{publication['source']}_"
                if publication["url"]:
                    line += f" ([link]({publication['url']}))"
                lines.append(line)
            remainder = len(section["publications"]) - len(shown)
            if remainder > 0:
                lines.append(f"- …and {remainder} more")

        if section["contradictions"]:
            lines.append("\n### Contested")
            for entry in section["contradictions"][:5]:
                a, b = entry["claim_a"], entry["claim_b"]
                lines.append(
                    f"- \"{(a['text'] or '')[:110]}\" **vs** \"{(b['text'] or '')[:110]}\""
                    f" _(confidence {entry['confidence']},"
                    f" {entry['prediction_mode']})_"
                )

        if section["entity_surges"]:
            surging = ", ".join(
                f"{surge['name']} ({surge['mentions']}×, baseline"
                f" {surge['baseline_mentions']})"
                for surge in section["entity_surges"][:5]
            )
            lines.append(f"\n**Surging:** {surging}")

        lines.append("")

    return "\n".join(lines).strip() + "\n"
