"""Daily cross-domain brief, built only through the KB contract (#960)."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from src.kb import contract

DEFAULT_BUDGET = 15


def _default_since() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_research_domain(definition) -> bool:
    return "papers" in definition.tags or "research" in definition.tags


def _cluster_line(cluster: Dict[str, Any], note: str = "") -> Dict[str, Any]:
    representative = cluster["representative"]
    sources = sorted({
        str(c.get("source") or "").strip() for c in cluster.get("citations", [])
        if c.get("source")
    })
    corroboration = cluster.get("corroboration", 1)
    return {
        "text": (representative.get("claim_text") or "").strip(),
        "url": representative.get("url"),
        "document_id": representative.get("document_id"),
        "sources": sources,
        "corroboration": corroboration,
        "note": note,
        "contradicted": bool(cluster.get("contradictions")),
        "recency": cluster.get("last_ingested_ms", 0),
        "confidence": representative.get("confidence"),
        "prediction_mode": representative.get("prediction_mode") or "unknown",
        "why_surfaced": note or (
            f"corroborated by {corroboration} independent sources"
            if corroboration > 1 else "new claim in the watch window"
        ),
        "depth": [
            {"document_id": c.get("document_id"), "title": c.get("title"),
             "source": c.get("source"), "url": c.get("url"),
             "cited": bool(c.get("document_id"))}
            for c in cluster.get("citations", [])
            if c.get("source_type") in {"paper", "book"}
        ],
    }


def generate_brief(
    domains: Optional[List[str]] = None,
    since: Optional[str] = None,
    budget: int = DEFAULT_BUDGET,
    conn=None,
    config_path=None,
) -> Dict[str, Any]:
    """Return markdown, structured sections, and budget/freshness metadata.

    One global hard cap covers every rendered entry—not only claims. Ranking
    favours contested and integrity-critical changes, then stance shifts,
    corroborated claims, publications, and surges. Nothing is silently omitted.
    """
    from src.kb.registry import load_registry

    registry = load_registry(config_path)
    names = domains or registry.names()
    since = since or _default_since()
    sections: List[Dict[str, Any]] = []
    candidates: List[Dict[str, Any]] = []

    for name in names:
        definition = registry.get(name)
        diff = contract.kb_diff(name, since, conn=conn, config_path=config_path)["data"]
        section: Dict[str, Any] = {
            "domain": name,
            "research": _is_research_domain(definition),
            "documents_new": diff["documents"]["new"],
            "documents_total": diff["documents"]["total"],
            "sources_delivered": diff["documents"]["sources_delivered"],
            "items": [], "contradictions": [], "stance_shifts": [],
            "entity_surges": [], "integrity_findings": [], "publications": [],
        }

        if section["research"]:
            documents = contract.kb_documents(
                name, since=since, limit=200, conn=conn, config_path=config_path
            )["data"]
            for document in documents:
                candidates.append({
                    "kind": "publications", "domain": name, "priority": 3,
                    "title": (document.get("title") or "(untitled)").strip(),
                    "source": document.get("source_id") or "",
                    "url": document.get("url"),
                    "document_id": document.get("document_id"),
                    "recency": document.get("ingested_at") or 0,
                    "prediction_mode": "source-record", "confidence": 1.0,
                    "why_surfaced": "new research publication in the watch window",
                })
        else:
            for cluster in diff.get("new_clusters", []):
                item = _cluster_line(cluster)
                if item["text"]:
                    candidates.append({**item, "kind": "items", "domain": name, "priority": 4})
            for cluster in diff.get("gained_corroboration", []):
                note = "gained sources: " + ", ".join(cluster.get("new_sources", []))
                item = _cluster_line(cluster, note=note)
                if item["text"]:
                    candidates.append({**item, "kind": "items", "domain": name, "priority": 4})

        for entry in diff.get("new_contradictions") or []:
            candidates.append({**entry, "kind": "contradictions", "domain": name,
                               "priority": 6, "recency": entry.get("created_at_ms", 0),
                               "why_surfaced": "newly contested claim pair"})
        for shift in diff.get("stance_shifts") or []:
            candidates.append({**shift, "kind": "stance_shifts", "domain": name,
                               "priority": 5, "recency": shift.get("detected_at_ms", 0),
                               "why_surfaced": "source stance changed across adjacent windows"})
        for surge in diff.get("entity_surges") or []:
            candidates.append({**surge, "kind": "entity_surges", "domain": name,
                               "priority": 2, "recency": diff.get("meta", {}).get("as_of_ms", 0),
                               "why_surfaced": "mention rate exceeded twice its trailing baseline"})
        for finding in (diff.get("integrity") or {}).get("findings", []):
            candidates.append({**finding, "kind": "integrity_findings", "domain": name,
                               "priority": 6 if finding.get("severity") == "high" else 3,
                               "recency": diff.get("meta", {}).get("as_of_ms", 0),
                               "prediction_mode": "rule:integrity-ledger",
                               "confidence": None,
                               "why_surfaced": "new integrity-ledger finding"})
        sections.append(section)

    candidates.sort(
        key=lambda row: (row.get("priority", 0), row.get("corroboration", 0),
                         row.get("recency", 0)), reverse=True
    )
    actual_budget = max(0, min(int(budget), DEFAULT_BUDGET))
    kept = candidates[:actual_budget]
    dropped = max(0, len(candidates) - len(kept))
    by_domain = {section["domain"]: section for section in sections}
    for item in kept:
        by_domain[item["domain"]][item["kind"]].append(item)

    quality_line = None
    if names:
        coverage = contract.kb_coverage(names[0], conn=conn, config_path=config_path)["data"]
        quality = coverage.get("evidence_quality")
        if quality and quality.get("total_rows"):
            quality_line = (
                f"{round(quality['model_grade_fraction'] * 100)}% of underlying analysis is "
                f"model-grade ({quality['total_rows']} prediction rows)"
            )
    meta = {
        "since": since, "generated_at_ms": int(time.time() * 1000),
        "budget": actual_budget, "kept": len(kept), "dropped": dropped,
        "eligible": len(candidates), "evidence_quality": quality_line,
        "watchlist": names,
    }
    return {"markdown": _render_markdown(sections, meta), "sections": sections, "meta": meta}


def _render_markdown(sections: List[Dict[str, Any]], meta: Dict[str, Any]) -> str:
    def cite(source: Optional[str], url: Optional[str], document_id: Optional[str]) -> str:
        label = source or document_id or "uncited — flagged"
        if url:
            return f"[{label}]({url})"
        if document_id:
            return f"{label} (`{document_id}`)"
        return "uncited — flagged"

    def evidence(rows: List[Dict[str, Any]]) -> str:
        return ", ".join(cite(r.get("source"), r.get("url"), r.get("document_id"))
                         for r in rows) if rows else "uncited — flagged"

    def mode(entry: Dict[str, Any]) -> str:
        confidence = entry.get("confidence")
        extra = f", confidence {confidence:.3f}" if isinstance(confidence, (int, float)) else ""
        return f"{entry.get('prediction_mode') or 'unknown'}{extra}"

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    subtitle = [f"since {meta['since']}", f"{meta['kept']} items"]
    if meta["dropped"]:
        subtitle.append(f"{meta['dropped']} below the budget line (not shown)")
    if meta["evidence_quality"]:
        subtitle.append(meta["evidence_quality"])
    lines = [f"# Noesis daily brief — {day}", "", "_" + " · ".join(subtitle) + "_", ""]

    for section in sections:
        collections = ("items", "publications", "contradictions", "stance_shifts",
                       "entity_surges", "integrity_findings")
        has_content = any(section[key] for key in collections)
        lines.append(
            f"## {section['domain']} — {section['documents_new']} new of "
            f"{section['documents_total']} documents"
        )
        if not has_content:
            note = ("nothing new (no arrivals from any feed)" if not section["sources_delivered"]
                    else "arrivals, but nothing cleared the bar")
            lines += [f"_{note}_", ""]
            continue

        for item in section["items"]:
            source = ", ".join(item["sources"]) or None
            line = (f"- **{item['text']}**  \n  {item['corroboration']}× corroborated — "
                    f"{cite(source, item.get('url'), item.get('document_id'))}")
            if item.get("note"):
                line += f" — _{item['note']}_"
            if item.get("contradicted"):
                line += " — ⚡ contested"
            line += f"  \n  Why: {item['why_surfaced']} · mode: `{mode(item)}`"
            if item.get("depth"):
                line += f"  \n  Depth: {evidence(item['depth'])}"
            lines.append(line)

        if section["publications"]:
            lines.append(f"\n### New publications ({len(section['publications'])})")
            for publication in section["publications"]:
                lines.append(
                    f"- {publication['title']} — {cite(publication['source'], publication['url'], publication['document_id'])}  \n"
                    f"  Why: {publication['why_surfaced']} · mode: `{mode(publication)}`"
                )

        if section["contradictions"]:
            lines.append("\n### Contested")
            for entry in section["contradictions"]:
                a, b = entry["claim_a"], entry["claim_b"]
                lines.append(
                    f"- \"{(a.get('text') or '')[:110]}\" **vs** \"{(b.get('text') or '')[:110]}\" — "
                    f"{cite(a.get('source'), a.get('url'), a.get('document_id'))} / "
                    f"{cite(b.get('source'), b.get('url'), b.get('document_id'))}  \n"
                    f"  Why: {entry['why_surfaced']} · mode: `{mode(entry)}`"
                )

        if section["stance_shifts"]:
            lines.append("\n### Stance shifts")
            for shift in section["stance_shifts"]:
                lines.append(
                    f"- **{shift['source']}** on {shift['topic']}: {shift['from_stance']} → {shift['to_stance']} — "
                    f"{evidence(shift.get('evidence', []))}  \n"
                    f"  Why: {shift['why_surfaced']} · mode: `{mode(shift)}`"
                )

        if section["entity_surges"]:
            lines.append("\n### Entity surges")
            for surge in section["entity_surges"]:
                lines.append(
                    f"- **{surge['name']}**: {surge['mentions']} mentions (baseline {surge['baseline_mentions']}) — "
                    f"{evidence(surge.get('evidence', []))}  \n"
                    f"  Why: {surge['why_surfaced']} · mode: `{mode(surge)}`"
                )

        if section["integrity_findings"]:
            lines.append("\n### Integrity")
            for finding in section["integrity_findings"]:
                label = finding.get("change_class") or finding.get("kind", "finding")
                lines.append(
                    f"- **{label}** for `{finding.get('document_id', 'unknown')}` — "
                    f"{evidence(finding.get('evidence', []))}  \n"
                    f"  Why: {finding['why_surfaced']} · mode: `{mode(finding)}`"
                )
        lines.append("")

    return "\n".join(lines).strip() + "\n"
