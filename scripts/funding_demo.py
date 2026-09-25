#!/usr/bin/env python3
"""Reproducible Funding & Grants shortlist and preparation demo.

Uses a clearly synthetic applicant profile and, by default, the authored
offline fixtures in ``tests/fixtures/funding`` served through the real
adapters (``DurableHTTP`` with an injected transport). The output states which
evidence it used. It makes no eligibility guarantee or funding-success claim,
and it submits nothing.

    python scripts/funding_demo.py --output docs/examples/funding-shortlist-demo.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _fmt_deadline(item):
    deadline = item.get("next_deadline")
    if not deadline:
        return "rolling" if item["state"] == "rolling" else "none known"
    return deadline["text"] + ("" if deadline.get("instant") else " (timezone unknown)")


def _next_steps(item):
    if item["bucket"] == "apply_now":
        return "Start a workspace; prepare the checklist and budget."
    if item["bucket"] == "consider":
        missing = sorted({m for c in item["clarifications"] for m in c.get("missing_facts", [])})
        unparsed = [c["requirement_id"] for c in item["clarifications"] if c.get("result") == "unparsed" and c.get("requirement_id")]
        parts = []
        if missing:
            parts.append("state " + ", ".join(missing))
        if unparsed:
            parts.append(f"clarify {len(unparsed)} requirement(s) with the funder's text or a reviewed interpretation")
        return "; ".join(parts) or "Resolve open clarifications."
    if item["record_kind"] == "directory_entry":
        return "Follow the link to the administering body's current call."
    if item["bucket"] == "watch":
        return "Monitor for an opening or dated round."
    return "Not applicable: " + ("; ".join(item["disqualifiers"]) or item["state"])


def build(output):
    from src.kb.funding_ranking import ShortlistService
    from src.kb.funding_workspaces import FundingDraftService, FundingWorkspaceStore
    from tests.unit.funding.harness import NS, SCOPES, Env, founder_profile

    env = Env("2026-09-25T09:00:00+00:00")
    env.acquire_all()
    profile = founder_profile(env)
    shortlist = ShortlistService(env.conn, now=env.now).build(NS, profile["profile_id"], principal_id="alice", scopes=SCOPES)
    top = next(i for i in shortlist["items"] if i["bucket"] == "apply_now")
    workspace = FundingWorkspaceStore(env.conn, now=env.now).create(
        NS, "demo", shortlist_id=shortlist["shortlist_id"], opportunity_id=top["opportunity_id"], principal_id="alice", scopes=SCOPES)
    draft = FundingDraftService(env.conn, now=env.now).generate(NS, workspace["workspace_id"], "demo", principal_id="alice", scopes=SCOPES)

    lines = [
        "# Funding & Grants — explained shortlist demo",
        "",
        "> **Evidence: offline authored fixtures only.** Provider payloads come from `tests/fixtures/funding` "
        "(hand-written to mirror provider page shapes; identifiers and amounts are illustrative). They are **not** "
        "live captures and say nothing about calls that are open today. Live checks are recorded separately in "
        "`docs/development/funding-evidence/`.",
        ">",
        "> The applicant is **synthetic**. Eligibility is a requirements assessment against stated facts, not a funder "
        "decision; the match score orders options and is **not** a probability of funding. Nothing was submitted.",
        "",
        f"Regenerate with `python scripts/funding_demo.py --output {output.as_posix()}` (as of 2026-09-25 09:00 UTC).",
        "",
        "## Synthetic applicant",
        "",
        "Three-person graduate team in Germany, university-affiliated, not yet incorporated, building an open-source "
        "search index (prototype). Needs EUR 60,000 over 12 months; prefers medium effort; timezone Europe/Berlin.",
        f"Unknown profile facts (never defaulted): {', '.join(shortlist['unknown_profile_facts'])}.",
        "",
        "## Shortlist",
        "",
        "| Bucket | Opportunity | Provider | Status | Next deadline | Verdict | Score (coverage) | Effort |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in shortlist["items"]:
        score = "—" if item["match_score"] is None else f"{item['match_score']:.2f} ({item['score_coverage']:.0%})"
        lines.append(f"| {item['bucket']} | [{item['title']}]({item['source_url']}) | {item['provider']} | {item['state']} | "
                     f"{_fmt_deadline(item)} | {item['verdict']} | {score} | {item['effort']} |")
    lines += ["", "## Why each option is where it is", ""]
    for item in shortlist["items"]:
        lines += [f"### {item['title']} — {item['bucket']}", ""]
        for name, criterion in item["criteria"].items():
            value = "unknown" if criterion["score"] is None else f"{criterion['score']:.2f}"
            lines.append(f"- **{name}** ({value}, weight {criterion['weight']}): {'; '.join(criterion['reasons'])}")
        if item["disqualifiers"]:
            lines.append(f"- **Disqualifiers:** {', '.join(item['disqualifiers'])}")
        for clarification in item["clarifications"][:4]:
            reason = clarification.get("reason") or ", ".join(clarification.get("missing_facts") or []) or clarification["result"]
            lines.append(f"- **Needs clarification** ({clarification.get('requirement_id') or clarification['result']}): {reason}")
        if item["state_reasons"]:
            lines.append(f"- **Status notes:** {'; '.join(item['state_reasons'])}")
        lines += [f"- **Next step:** {_next_steps(item)}", ""]
    lines += [
        f"## Preparation workspace — {top['title']}",
        "",
        "Checklist derived from the cited call revision (status as created):",
        "",
    ]
    for entry in workspace["items"]:
        lines.append(f"- [ ] ({entry['kind']}) {entry['text']}")
    budget = draft["budget"]
    lines += [
        "",
        "Draft application (authored report, editable):",
        "",
        f"- Unanswered items left explicit: {', '.join(draft['unanswered']) or 'none'}",
        f"- Budget: total {budget['total']} {budget['currency']}, eligible {budget['eligible_total']}, requested "
        f"{budget['requested']}, co-financing {budget['co_financing']} (calculation receipt `{budget['calculation_id']}`)",
    ]
    lines += [f"- Budget issue: {issue}" for issue in draft["budget_issues"]]
    lines += ["", "No application was submitted and no funder was contacted.", ""]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines))
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(build(parser.parse_args().output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
