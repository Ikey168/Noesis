#!/usr/bin/env python3
"""Reproducible, explained ownership-dossier demo from offline fixtures.

Runs the real corporate-ownership source pack through ``SourcePackRuntime``
with the pinned *authored* fixtures in ``tests/fixtures/ownership`` compiled
into network-free adapters, reviews the proposed identity candidates with the
recorded rule below (accept identifier-based and name matches, reject the
decoy that shares only a name), and renders a dossier for one identifier. The
companies, people and filings are fictional; the output says so and is
**offline evidence only**. It makes no beneficial-ownership, sanctions or AML
determination.

    python scripts/ownership_demo.py --output docs/examples/ownership-dossier-demo.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _figure(edge):
    share = edge.get("share") or {}
    if share.get("exact"):
        return f"{share['exact']}%"
    band = share.get("band")
    if band:
        low = f"{'≥' if band.get('min_inclusive') else '>'}{band['min']}%" if band.get("min") else ""
        high = f"{'≤' if band.get('max_inclusive') else '<'}{band['max']}%" if band.get("max") else ""
        return f"band {low} {high}".strip()
    return "no figure stated"


def _validity(edge):
    v = edge["validity"]
    end = v["to"] or {"open": "open", "unknown": "unknown"}[v["to_status"]]
    return f"{v['from'] or 'unknown'} → {end}"


def _holder(edge, dossier):
    holder = edge.get("holder") or {}
    if holder.get("name"):
        return holder["name"]
    names = {n["record_key"]: n["name"] for e in dossier["entities"] for n in e["names"] if not n["redacted"]}
    return names.get(holder.get("key")) or ("person (owner-scoped)" if holder.get("kind") == "person" else holder.get("key")) or "unidentified"


def render(dossier, *, as_of, lei, env_note):
    lines = [
        "# Corporate Ownership — explained dossier demo",
        "",
        "> **Evidence: offline authored fixtures only.** Every provider response comes from `tests/fixtures/ownership` "
        "(hand-written to mirror GLEIF, Companies House, SEC EDGAR and Open Ownership response shapes). The "
        "companies, officers, people and filings are **fictional**; identifiers are illustrative. This is **not** a "
        "live capture and not a dossier of any real company. Live checks are recorded separately in "
        "`docs/development/ownership-evidence/` (the 2026-09-27 run could not reach any provider).",
        ">",
        "> Ownership assertions are what each source states. Conflicts are shown, not resolved. Nothing here is a "
        "beneficial-ownership, sanctions or AML determination.",
        "",
        f"Regenerate with `python scripts/ownership_demo.py --output docs/examples/ownership-dossier-demo.md`. "
        f"Query: LEI `{lei}`, as of {as_of}. {env_note}",
        "",
        "## Identity: which source records were grouped, and why",
        "",
        "Grouping happens only through reviewed identity decisions (recorded in `src/kb/entity_history.py`); no "
        "record was merged or rewritten.",
        "",
        "| Candidate | Basis | Confidence | Decision |",
        "| --- | --- | --- | --- |",
    ]
    for c in dossier["identity"]["candidates"]:
        lines.append(f"| `{c['left_key']}` ↔ `{c['right_key']}` | {c['basis']} | {c['confidence']} | {c['state']} |")
    lines += ["", "## Entities", ""]
    for entity in dossier["entities"]:
        names = "; ".join(f"{n['name']} ({n['provider']}{', ' + n['jurisdiction'] if n['jurisdiction'] else ''})"
                          for n in entity["names"] if not n["redacted"]) or "(not acquired)"
        lines.append(f"- `{entity['entity']}` — {names}")
    parents = dossier["relationships"]["direct_parents"]
    lines += ["", f"## Direct parents and control as of {as_of}", "",
              "| Holder | Kind | Figure | Validity | As-of status | Source |", "| --- | --- | --- | --- | --- | --- |"]
    for group in parents["groups"]:
        for edge in group["assertions"]:
            lines.append(f"| {_holder(edge, dossier)} | {edge['assertion_kind']} ({edge['control_basis']}) | "
                         f"{_figure(edge)} | {_validity(edge)} | {edge['as_of_status']} | {edge['source']['provider']} |")
    others = parents["other_holdings"]["person_or_unidentified_holders"]
    for edge in others:
        lines.append(f"| {_holder(edge, dossier)} (person; owner-scoped) | {edge['assertion_kind']} | {_figure(edge)} | "
                     f"{_validity(edge)} | {edge['as_of_status']} | {edge['source']['provider']} |")
    lines += ["", "Excluded as of this date (kept, not deleted): " + ", ".join(
        f"{e['assertion_kind']} from {e['provider']} ({e['as_of_status']})" for e in parents["excluded"]) + ".", ""]
    lines += ["## Conflicts (returned together, never ranked)", ""]
    for conflict in dossier["conflicts"]:
        lines.append(f"- **{conflict['slot']}**: {', '.join('`' + h + '`' for h in conflict['holders'])} — "
                     f"{', '.join(conflict['reasons'])}. {conflict['resolution']}.")
    ultimate = dossier["relationships"]["ultimate_parents"]
    lines += ["", "## Ultimate parent", ""]
    for group in ultimate["groups"]:
        for edge in group["assertions"]:
            lines.append(f"- Stated by {edge['source']['provider']}: `{group['holder']}` ({edge['assertion_kind']}, "
                         f"{_validity(edge)}).")
    lines.append(f"- Derived chain top(s): {', '.join('`' + t + '`' for t in ultimate['derived_chain_tops']['tops'])} — "
                 f"{ultimate['derived_chain_tops']['note']}.")
    lines += ["", "## Reporting exceptions (a statement that an owner is not reported — not 'no owner')", ""]
    exceptions = dossier["reporting_exceptions"]
    holding = dossier.get("_holding_exceptions") or []
    for edge in exceptions + holding:
        lines.append(f"- `{edge['subject']}`: {edge['reporting_exception']['category']} "
                     f"({edge['reporting_exception']['level']}) per {edge['source']['provider']}, validity {_validity(edge)}.")
    successors = dossier["relationships"]["successor_chain"]
    lines += ["", "## Successors and predecessors (linked by events, never collapsed)", ""]
    for link in successors["predecessors"]["chain"] + successors["successors"]["chain"]:
        lines.append(f"- `{link['from']}` → `{link['to']}`: {link['event_type']}, date {link['event_date'] or 'unknown'} "
                     f"({link['source']['provider']}).")
    lines += ["", "## Officers", "", "| Officer | Role | Appointed | Resigned | Source |", "| --- | --- | --- | --- | --- |"]
    for officer in dossier["officers"]:
        appointed = officer["appointed_on"] or "unknown"
        if officer["appointed_status"] == "before":
            appointed = "before " + appointed
        lines.append(f"| {officer['officer']['name']} | {officer['role']} | {appointed} | "
                     f"{officer['resigned_on'] or '—'} | {officer['source']['provider']} |")
    lines += ["", "## Filings", "", "| Accession / transaction | Form | Filed | Parsed | Sources |", "| --- | --- | --- | --- | --- |"]
    for filing in dossier["filings"]:
        filed = next((s["filing_date"] for s in filing["statements"] if s["filing_date"]), "unknown")
        lines.append(f"| {filing['accession_number']} | {filing['form_type']} | {filed} | "
                     f"{'yes' if any(s['parsed'] for s in filing['statements']) else 'reference only'} | "
                     f"{', '.join(sorted({s['provider'] for s in filing['statements']}))} |")
    lines += ["", "## Timeline", "", "| Event time | Kind | What | Source |", "| --- | --- | --- | --- |"]
    for entry in dossier["timeline"]["entries"]:
        lines.append(f"| {entry['event_time']} | {entry['kind']} | {entry['summary']} | {entry['source']['provider']} |")
    lines += ["", "Undated (unknown, not interpolated):", ""]
    for entry in dossier["timeline"]["undated"]:
        lines.append(f"- {entry['kind']}: {entry['summary']} ({entry['source']['provider']})")
    lines += ["", "## Unknowns (listed, never defaulted)", ""]
    counted = {}
    for item in dossier["unknowns"]:
        key = (item["kind"], item["provider"] or "no source", ", ".join(item["unknown"]))
        counted[key] = counted.get(key, 0) + 1
    for (kind, provider, fields), count in sorted(counted.items()):
        lines.append(f"- {kind} ({provider}): {fields} — {count} record{'s' if count > 1 else ''}")
    listed = dossier.get("_listed")
    if listed:
        lines += ["", f"## The listed parent: {listed['name']}", "",
                  "Subsidiaries stated as of the same date, minority holdings disclosed on Schedule 13G cover pages "
                  "(holdings, not parents) and SEC filing references:", ""]
        for sub in listed["subsidiaries"]:
            kinds = sorted({f"{a['assertion_kind']} ({a['source']['provider']})" for a in sub["assertions"]})
            lines.append(f"- Subsidiary `{sub['subject']}`: {', '.join(kinds)}")
        for edge in listed["minority"]:
            lines.append(f"- Minority holding: {_holder(edge, dossier)} {_figure(edge)} of {edge['share'].get('class') or 'the class'} "
                         f"as of {edge['validity']['from']} (end {edge['validity']['to_status']}), per {edge['basis']}.")
        for filing in listed["filings"]:
            filed = next((s["filing_date"] for s in filing["statements"] if s["filing_date"]), "unknown")
            parsed = "cover page parsed" if any(s["parsed"] for s in filing["statements"]) else "reference only"
            lines.append(f"- {filing['form_type']} {filing['accession_number']} filed {filed} ({parsed}).")
    lines += ["", "## Replay", "",
              f"Dossier hash `{dossier['dossier_hash']}` over {len(dossier['pins']['records'])} pinned record revisions "
              f"and {len(dossier['pins']['identity'])} accepted identity decisions; evidence kind "
              f"`{dossier['evidence']['kind']}`.", ""]
    return "\n".join(lines)


def build(*, as_of="2025-06-01"):
    from src.kb.ownership_dossier import build_dossier
    from src.kb.ownership_graph import OwnershipGraph
    from src.kb.ownership_identity import OwnershipIdentityService
    from tests.unit.ownership import harness

    env = harness.Env()
    env.install()
    env.acquire("demo")
    market = harness.seed_market(env.conn)
    service = OwnershipIdentityService(env.conn, now=env.now)
    for item in service.propose(harness.NS, principal_id=harness.PRINCIPAL, scopes=harness.SCOPES, market=market,
                                lei_namespace=harness.NS)["candidates"]:
        decoy = harness.DECOY in (item["left_key"], item["right_key"])
        service.review(harness.NS, item["candidate_id"], "reject" if decoy else "accept",
                       "decoy shares only a name" if decoy else "identifiers or reviewed name agree",
                       principal_id=harness.REVIEWER, scopes=harness.REVIEW_SCOPES)
    dossier = build_dossier(env.conn, harness.NS, "lei", harness.UK, principal_id=harness.PRINCIPAL,
                            scopes=harness.SCOPES, as_of=as_of, market=market, evidence_kind="offline-fixture")
    graph = OwnershipGraph(env.conn, harness.NS, principal_id=harness.PRINCIPAL, scopes=harness.SCOPES)
    parent = graph.direct_parents(harness.HOLD_KEYS["gleif"], as_of)
    dossier["_holding_exceptions"] = parent["reporting_exceptions"]
    listed = build_dossier(env.conn, harness.NS, "cik", "0009999101", principal_id=harness.PRINCIPAL,
                           scopes=harness.SCOPES, as_of=as_of, market=market, evidence_kind="offline-fixture")
    dossier["_listed"] = {
        "name": "Exampla Holdings plc (fictional; CIK 0009999101)",
        "subsidiaries": listed["relationships"]["subsidiaries"]["subsidiaries"],
        "minority": listed["relationships"]["direct_parents"]["other_holdings"]["minority_as_stated"],
        "filings": [f for f in listed["filings"] if any(s["provider"] == "sec-edgar" for s in f["statements"])],
    }
    return dossier, harness.UK


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--as-of", default="2025-06-01")
    args = parser.parse_args()
    dossier, lei = build(as_of=args.as_of)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(dossier, as_of=args.as_of, lei=lei,
                                  env_note="Reviewer decisions follow the rule stated in the script docstring."))
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
