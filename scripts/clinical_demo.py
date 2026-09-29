#!/usr/bin/env python3
"""Reproducible Clinical Evidence map demo (offline, authored fixtures).

Runs the whole journey for one clinical question over the authored fixtures in
``tests/fixtures/clinical`` through the real source-pack runtime, adapters and
stores (injected transports; nothing leaves the process) and writes a cited
evidence map as Markdown. The intervention and every identifier are fictional.
The output is not medical advice and makes no dosing or treatment
recommendation. Live coverage is recorded separately in
``docs/development/clinical-evidence/``.

    python scripts/clinical_demo.py --output docs/examples/clinical-evidence-map-demo.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _cell(value):
    if value is None or value == [] or value == "":
        return "unknown"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value).replace("|", "/")


def build(output: Path) -> str:
    from src.kb.clinical_evidence import EvidenceMapService
    from tests.unit.clinical.harness import NS, QUESTION, Env

    env = Env()
    env.journey()
    view = env.build_map(request_key="demo")
    bundle = EvidenceMapService(env.conn).export_bundle(NS, view["view_id"], scopes=env.scopes())["bundle"]
    strength = view["strength"]
    lines = [
        "# Clinical Evidence: cited evidence-map demo",
        "",
        "> **Evidence: offline authored fixtures only.** Provider responses come from `tests/fixtures/clinical` "
        "(hand-written in each provider's response shape). The intervention *noetiglutide*, its sponsor, every "
        "trial number, PMID, DOI, label and product number are **fictional placeholders**. Nothing here describes "
        "a real trial or medicine. Live checks are recorded separately in `docs/development/clinical-evidence/`; the "
        "latest one was blocked by the build environment's network policy, so no source is live-verified.",
        ">",
        f"> **{view['boundary']}**",
        "",
        f"Regenerate with `python scripts/clinical_demo.py --output {output.as_posix()}`.",
        "",
        "## Question",
        "",
        f"- Population: {QUESTION['population']}",
        f"- Condition: {QUESTION['condition']} (expanded to MeSH {', '.join(view['expansion']['condition']['mesh_ids'])})",
        f"- Intervention: {QUESTION['intervention']} (no MeSH mapping; matched as registered)",
        f"- Comparator: {QUESTION['comparator']}; outcome of interest: {', '.join(QUESTION['outcomes'])}",
        "",
        "Term expansion, explained:",
        "",
    ]
    for step in view["expansion"]["condition"]["steps"]:
        lines.append(f"- `{step['concept_id']}`: {step['explanation']}")
    lines += ["", "## Trials", "",
              "| Registry | Identifier | Status | Revisions | Phase | Allocation / masking | Planned / actual N | "
              "Results | Publications | Retracted | Outcome-switching findings |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for trial in view["trials"]:
        design = trial["design"]
        lines.append(
            f"| {trial['registry']} | [{trial['identifier']}]({trial['source_url']}) | {_cell(trial['status']['normalized'])} "
            f"| {len(trial['versions'])} | {_cell(design['phase'])} | {_cell(design['allocation'])} / "
            f"{_cell(design['masking'])} | {_cell(design['sample_size_planned'])} / {_cell(design['sample_size_actual'])} "
            f"| {trial['results']['state']} | {len(trial['publications'])} | {_cell(trial['retracted'])} "
            f"| {', '.join(f['kind'] for f in trial['outcome_switching']) or 'none recorded'} |")
    lines += ["", "Registered twice, never merged (cross-registry links with their identifier evidence):", ""]
    for trial in view["trials"]:
        for peer in trial["cross_registry"]:
            fields = ", ".join(d["field"] for d in peer["disagreements"]) or "none"
            lines.append(f"- {trial['identifier']} <-> {peer['registry']} {peer['identifier']}: disagreements on {fields}")
    lines += ["", "## Outcome-switching findings (findings, not verdicts)", ""]
    for trial in view["trials"]:
        for finding in trial["outcome_switching"]:
            after = finding["after"]
            where = (f"{after.get('registry_version')} ({after.get('time_frame')})" if after.get("registry_version")
                     else f"publication {after['publication']['document_id']}: \"{after['text']}\"")
            lines.append(f"- {trial['identifier']} `{finding['kind']}` {finding['measure']}: "
                         f"{finding['before']['registry_version']} ({finding['before']['time_frame']}) -> {where}")
    lines += ["", "## Regulatory records (providers' own disclaimers kept)", ""]
    for item in view["regulatory"]:
        version = item["native_version"].get("version") or item["native_version"].get("date")
        lines.append(f"- {item['authority']} {item['regulatory_kind']}: {item['title']} (version {version}) "
                     f"[{item['source_url']}]")
        if item.get("counts"):
            top = ", ".join(f"{c['term']} {c['reports']}" for c in item["counts"][:3])
            lines.append(f"  - FAERS report counts (reports mentioning the term, of {item['reports_total']} reports): "
                         f"{top}. {item['count_semantics']}")
        if item.get("disclaimer"):
            lines.append(f"  - openFDA disclaimer: \"{item['disclaimer']['text']}\"")
        if item.get("omitted_sections"):
            lines.append("  - Not retained: " + ", ".join(o["section"] for o in item["omitted_sections"])
                         + " (outside the non-advice boundary)")
    lines += ["", "## Registered reviews", ""]
    for review in view["reviews"]:
        lines.append(f"- {review['identifier']}: {review['title']} ({review['status'].get('native')}; "
                     f"{review['note']}; supplied as a user export)")
    lines += ["", "## Strength of the evidence base (rule-based, traceable)", "",
              f"- Summary rule **{strength['summary']['rule']}**: {strength['summary']['description']}.",
              f"- Counts: {strength['counts']}.",
              f"- Score: none computed. GRADE asserted: {strength['grade']['asserted']} "
              f"(inputs missing: {', '.join(strength['grade']['inputs_missing'])}).",
              "- Per-trial design rules:"]
    for row in strength["trials"]:
        lines.append(f"  - {row['identifier']}: {row['design_rule']} {row['design_category']} "
                     f"(inputs {row['design_inputs']}); results rule {row['results_rule']} ({row['results']})")
    lines += ["", "## Unknowns and coverage gaps", ""]
    lines.append("- Unknown fields across trials: " + ", ".join(strength["unknowns"]))
    gaps = view["coverage_gaps"]
    for item in gaps["unregistered_publications"]:
        lines.append(f"- Unregistered publication: {item['title']} (PMID {item['identifiers']['pmid']}): {item['reason']}")
    for item in gaps["unmapped_terms"]:
        lines.append(f"- Unmapped question term {item['term']!r}: {item['reason']}")
    for item in gaps["unlinked_trials"]:
        lines.append(f"- Unlinked trial {item['identifier']}: {item['reason']}")
    lines += ["", "## Evidence bundle", "",
              f"- `{bundle['bundle_id']}` ({bundle['manifest']['entry_count']} objects, completeness "
              f"{bundle['completeness']['status']}: {len(bundle['completeness']['omissions'])} declared omissions).",
              f"- Evidence kind: offline only = {view['evidence_kind']['offline_only']}; live providers: "
              f"{view['evidence_kind']['live_providers'] or 'none'}.", ""]
    text = "\n".join(lines)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text)
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.output)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
