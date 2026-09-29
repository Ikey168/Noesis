#!/usr/bin/env python3
"""Reproducible Public Procurement shortlist and bid-preparation demo (offline).

Uses a clearly synthetic supplier profile ("Nordlicht Fixture IT GmbH") and the
authored offline fixtures of the ``procurement`` source pack, run through the
real source-pack runtime and native adapters with fixture transports. The
output states which evidence it used. It makes no eligibility guarantee or
win-probability claim, contacts no buyer and submits nothing.

    python scripts/procurement_demo.py --output docs/examples/procurement-shortlist-demo.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

AS_OF = "2026-09-27T09:00:00+00:00"


def _deadline(item):
    deadline = item.get("next_deadline")
    if not deadline:
        return "none known"
    return f"`{deadline['text']}`" + ("" if deadline.get("instant") else " (no offset stated)")


def _value(item):
    value = item.get("estimated_value")
    if not value or value.get("amount") is None:
        return "unknown"
    return f"{int(value['amount']):,} {value['currency']} est., VAT {value.get('vat', 'unknown')}"


def _score(item):
    return "—" if item["match_score"] is None else f"{item['match_score']:.2f} ({round(item['score_coverage'] * 100)}%)"


def _next_step(item):
    if item["bucket"] == "apply_now":
        return "Start a bid workspace; work the cited checklist before the published deadline."
    if item["bucket"] == "consider":
        missing = sorted({m for c in item["clarifications"] for m in c.get("missing_facts", [])})
        unparsed = [c["requirement_id"] for c in item["clarifications"] if c.get("result") == "unparsed" and c.get("requirement_id")]
        parts = []
        if missing:
            parts.append("state " + ", ".join(missing))
        if unparsed:
            parts.append(f"read {len(unparsed)} criterion/criteria in the procurement documents (not machine-checkable)")
        if any(not c.get("requirement_id") for c in item["clarifications"]):
            parts.append("the notice states no machine-readable criteria; read the procurement documents")
        if item["criteria"]["cpv_fit"]["score"] == 0.0:
            parts.append("outside your CPV interests")
        if item["criteria"]["jurisdiction"]["score"] == 0.0:
            parts.append("outside your stated jurisdictions")
        return "; ".join(parts) or "Resolve open clarifications."
    if item["bucket"] == "watch":
        return "Monitor for the contract notice."
    return "Not applicable: " + ("; ".join(item["disqualifiers"]) or item["state"])


def build(output):
    from src.kb.procurement_identity import ProcurementIdentityService
    from src.kb.procurement_ranking import ShortlistService
    from src.kb.procurement_workspaces import ProcurementWorkspaceStore
    from tests.unit.procurement.harness import BUYER, NS, SCOPES, Env, supplier_profile

    env = Env(AS_OF)
    receipt = env.acquire()
    profile = supplier_profile(env)
    shortlist = ShortlistService(env.conn, now=env.now).build(NS, profile["profile_id"], principal_id="alice", scopes=SCOPES)
    top = next(i for i in shortlist["items"] if i["bucket"] == "apply_now")
    workspace = ProcurementWorkspaceStore(env.conn, now=env.now).create(
        NS, "demo-bid", shortlist_id=shortlist["shortlist_id"], item_id=top["item_id"], principal_id="alice", scopes=SCOPES)
    incumbency = ProcurementIdentityService(env.conn, now=env.now).incumbency(
        NS, scopes=SCOPES, buyer=BUYER, cpv=["72253000"], supplier_names=["Nordlicht Fixture IT GmbH"])
    executions = sorted({env.notices().provider_state(NS, p)["last_execution"] for p in ("ted", "uk-fts", "uk-cf", "sam-gov")})

    lines = [
        "# Public Procurement — explained shortlist demo (offline)",
        "",
        "> **Evidence: offline authored fixtures only.** Notices come from `tests/fixtures/source_packs/procurement-*.json` "
        "(hand-written to mirror TED eForms search, UK OCDS and SAM.gov responses; buyers, suppliers and identifiers are fictional). "
        f"They ran through the real source-pack runtime with fixture transports (receipt execution: {', '.join(executions)}). "
        "They are **not** live captures and say nothing about procedures open today. Live checks are recorded separately in "
        "`docs/development/procurement-evidence/` (the 2026-09-27 run was blocked for every provider).",
        ">",
        "> The supplier is **synthetic**. Eligibility is a requirements assessment against stated facts and cited notice text, not the "
        "contracting authority's decision; the match score orders options and is **not** a probability of winning. Award history is "
        "context only. Nothing was submitted and no buyer or portal was contacted.",
        "",
        f"Regenerate with `python scripts/procurement_demo.py --output docs/examples/procurement-shortlist-demo.md` (as of {AS_OF[:16].replace('T', ' ')} UTC; "
        f"source-pack run status: {receipt['status']}).",
        "",
        "## Synthetic supplier",
        "",
        "Nordlicht Fixture IT GmbH: a small German IT service company (38 staff, turnover EUR 1.5 million), CPV interests 72250000 "
        "(system and support services), 72222300 (IT services) and 72400000 (internet services); jurisdictions DE and AT; certified "
        "ISO 9001 and ISO/IEC 20000-1; four comparable references; self-declares that five exclusion grounds do not apply; contract "
        "range EUR 50,000–1,000,000; needs at least 14 days to prepare; timezone Europe/Berlin.",
        "",
        "Unknown profile facts (never defaulted): " + ", ".join(shortlist["unknown_profile_facts"]) + ".",
        "",
        "## Shortlist (one row per lot)",
        "",
        "| Bucket | Procedure / lot | Provider | State | Deadline as published | Estimated value | Verdict | Score (coverage) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in shortlist["items"]:
        lot = f" — {item['lot_title']} ({item['lot_id']})" if item["lot_id"] else ""
        lines.append(f"| {item['bucket']} | [{item['title']}]({item['source_url']}){lot} | {item['provider']} | {item['state']} | "
                     f"{_deadline(item)} | {_value(item)} | {item['verdict']} | {_score(item)} |")
    lines += ["", "## Why each lot is where it is", ""]
    for item in shortlist["items"]:
        lot = f" — {item['lot_id']}" if item["lot_id"] else ""
        lines += [f"### {item['title']}{lot} — {item['bucket']}", "",
                  f"Notice `{item['notice']['notice_id']}` ({item['notice']['stage']}), procedure revision {item['revision']}, buyer {item['buyer']}.", ""]
        findings = [c for c in item["clarifications"] if c.get("requirement_id")]
        for criterion, detail in item["criteria"].items():
            score = "unknown" if detail["score"] is None else f"{detail['score']:.2f}"
            lines.append(f"- **{criterion}** ({score}, weight {detail['weight']}): {'; '.join(detail['reasons'])}")
        if item["disqualifiers"]:
            lines.append(f"- **Disqualifiers (cited):** {', '.join(item['disqualifiers'])}")
        for clarification in findings[:4]:
            reason = ", ".join(clarification.get("missing_facts") or []) or clarification.get("reason") or clarification["result"]
            lines.append(f"- **Unknown:** `{clarification['requirement_id']}` — {reason}")
        if item["award_context"]:
            context = "; ".join(f"{a['date']} {', '.join(a['suppliers']) or 'unstated supplier'} ({a['relation']}, notice {a['notice_id']})"
                                for a in item["award_context"][:3])
            lines.append(f"- **Award context (not evidence the procedure is open):** {context}")
        if item["state_reasons"]:
            lines.append(f"- **Status notes:** {'; '.join(item['state_reasons'])}")
        lines += [f"- **Next step:** {_next_step(item)}", ""]
    lines += ["## Eligibility with cited passages for the top lot", "",
              f"{top['title']} — {top['lot_title']} ({top['lot_id']}); every conclusion cites notice `{top['notice']['notice_id']}` revision {top['revision']}.", "",
              "| Requirement | Category | Result | Profile facts used | Cited passage |", "| --- | --- | --- | --- | --- |"]
    from src.kb.procurement_eligibility import ProcurementEligibilityService

    result = ProcurementEligibilityService(env.conn, now=env.now).assess(NS, profile["profile_id"], top["procedure_key"],
                                                                         principal_id="alice", scopes=SCOPES)
    for finding in result["lots"][top["lot_id"] or "_"]["findings"]:
        if not finding.get("requirement_id"):
            continue
        quote = (finding["citation"] or {}).get("quote") or ""
        lines.append(f"| `{finding['requirement_id']}` | {finding.get('category')} | {finding['result']} | "
                     f"{', '.join(finding.get('facts_used') or []) or '—'} | {quote[:110]} |")
    lines += ["", "## Incumbency (derived from award history)", "", incumbency["explanation"], "",
              f"_{incumbency['semantics']}._", "",
              "## Bid workspace checklist for the top lot", "",
              f"Workspace `{workspace['workspace_id']}` (a research project; {workspace['notice']})", "",
              "| Item | Kind | Text | Due / source |", "| --- | --- | --- | --- |"]
    for entry in workspace["items"]:
        due = entry.get("due") or entry["source"].get("url") or (entry["source"].get("locator") or {}).get("field") or ""
        lines.append(f"| `{entry['item_id']}` | {entry['kind']}{'/' + entry['document_kind'] if entry.get('document_kind') else ''} | "
                     f"{entry['text'][:110]} | {due} |")
    lines += ["", "## Sensitivity", "", "| Criterion change | Positions changed | Top changed |", "| --- | --- | --- |"]
    for key, value in shortlist["sensitivity"].items():
        lines.append(f"| {key} | {value['changed_positions']} | {value['top_changed']} |")
    lines.append("")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines))
    return shortlist


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    shortlist = build(args.output)
    print({bucket: len(ids) for bucket, ids in shortlist["buckets"].items()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
