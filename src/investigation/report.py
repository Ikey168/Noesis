"""
The case brief: an investigation rendered as a cited, readable finding.

Everything the brief states traces back to an evidence row, and every
evidence row carries its citation state - an uncited line is *flagged*, never
hidden (the same evidence discipline as the OSINT surface). The brief is a
structure (for panels, agents and the API), with a ``render_markdown`` helper
for humans.

Stdlib-only; the connection is injected read-only.
"""

from __future__ import annotations

from typing import Any, Dict, List

from src.investigation import store
from src.investigation.engine import hypothesis_matrix


def case_brief(conn, case_id: str) -> Dict[str, Any]:
    """The cited brief for one case: question, verdict (or the named gaps
    keeping the case open), the hypothesis ranking, the evidence behind the
    leader, unresolved contradictions, and what the engine did to get here."""
    case = store.get_case(conn, case_id)
    if case is None:
        return {"error": f"case {case_id!r} not found", "code": "not_found"}

    matrix = hypothesis_matrix(conn, case_id)
    evidence = store.list_evidence(conn, case_id)
    leads = store.list_leads(conn, case_id)
    journal = store.list_events(conn, case_id)

    leader_id = matrix.get("leader")
    key_evidence = [
        _evidence_line(row)
        for row in evidence
        if row["hypothesis_id"] == leader_id
        and row["relation"] in (store.RELATION_SUPPORTS, store.RELATION_CONTRADICTS)
        and not row["summary"].startswith("mirror:")
    ]
    contradictions = [
        _evidence_line(row) for row in evidence if row["kind"] == "contradiction"
    ]
    context = [
        _evidence_line(row)
        for row in evidence
        if row["relation"] == store.RELATION_CONTEXT
        and row["kind"] not in ("contradiction",)
    ]

    gaps: List[str] = [
        e["detail"]["gap"]
        for e in journal
        if e["event"] == "gap_noted" and e["detail"].get("gap")
    ]
    open_leads = [l for l in leads if l["status"] == store.LEAD_OPEN]
    if open_leads:
        gaps.append(f"{len(open_leads)} planned lead(s) not yet pursued")
    withheld = [e for e in journal if e["event"] == "conclusion_withheld"]
    if case["status"] != store.STATUS_CONCLUDED and withheld:
        gaps.extend(g for g in withheld[-1]["detail"].get("gaps", []) if g not in gaps)

    uncited = sum(1 for row in evidence if not row["cited"])
    return {
        "case_id": case_id,
        "question": case["question"],
        "status": case["status"],
        "verdict": case["verdict"],
        "verdict_hypothesis": case["verdict_hypothesis"],
        "hypotheses": [
            {
                "hypothesis_id": h["hypothesis_id"],
                "statement": h["statement"],
                "kind": h["kind"],
                "status": h["status"],
                "independent_support_count": h["independent_support_count"],
                "independent_contradict_count": h["independent_contradict_count"],
                "weighted_support": h["weighted_support"],
                "weighted_contradict": h["weighted_contradict"],
                "net": h["net"],
                "single_sourced": h["single_sourced"],
            }
            for h in matrix.get("hypotheses", [])
        ],
        "support_credibility": matrix.get("support_credibility"),
        "key_evidence": key_evidence,
        "contradictions": contradictions,
        "context": context,
        "gaps": gaps,
        "uncited_evidence_count": uncited,
        "leads_pursued": sum(1 for l in leads if l["status"] == store.LEAD_PURSUED),
        "leads_open": len(open_leads),
        "journal_length": len(journal),
        # The brief's own honesty line: what it rests on.
        "n": matrix.get("n", 0),
        "method": matrix.get("method"),
        "assumptions": matrix.get("assumptions", []),
    }


def _evidence_line(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "relation": row["relation"],
        "kind": row["kind"],
        "source": row["source"],
        "credibility": row["credibility"],
        "cited": row["cited"],
        "summary": row["summary"],
        "ref_id": row["ref_id"],
    }


def render_markdown(brief: Dict[str, Any]) -> str:
    """The brief as a human-readable markdown document. Uncited lines carry a
    visible ``[UNCITED]`` flag."""
    if brief.get("error"):
        return f"**error:** {brief['error']}"
    lines = [f"# Case brief: {brief['question']}", ""]
    if brief["verdict"]:
        lines += [f"**Verdict:** {brief['verdict']}", ""]
    else:
        lines += [f"**Status:** {brief['status']} - no verdict yet", ""]
        if brief["gaps"]:
            lines.append("**What is keeping this open:**")
            lines += [f"- {g}" for g in brief["gaps"]]
            lines.append("")

    lines.append("## Hypotheses")
    for h in brief["hypotheses"]:
        flag = " (single-sourced)" if h["single_sourced"] else ""
        lines.append(
            f"- `{h['hypothesis_id']}` {h['statement']} - support "
            f"{h['weighted_support']} across {h['independent_support_count']} "
            f"independent source(s), contradiction {h['weighted_contradict']}{flag}"
        )
    lines.append("")

    if brief["key_evidence"]:
        lines.append("## Key evidence")
        for e in brief["key_evidence"]:
            cite = "" if e["cited"] else " [UNCITED]"
            lines.append(f"- ({e['relation']}) {e['summary']}{cite}")
        lines.append("")
    if brief["contradictions"]:
        lines.append("## Where the record disagrees with itself")
        for e in brief["contradictions"]:
            cite = "" if e["cited"] else " [UNCITED]"
            lines.append(f"- {e['summary']}{cite}")
        lines.append("")
    if brief["gaps"] and brief["verdict"]:
        lines.append("## Noted gaps")
        lines += [f"- {g}" for g in brief["gaps"]]
        lines.append("")

    lines.append(
        f"_{brief['leads_pursued']} lead(s) pursued, {brief['leads_open']} open; "
        f"{brief['n']} independent source(s) weighed; "
        f"{brief['uncited_evidence_count']} uncited row(s) flagged._"
    )
    return "\n".join(lines)
