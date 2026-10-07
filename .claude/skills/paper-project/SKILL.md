---
name: paper-project
description: Plan and run an academic paper end to end in Noesis — from research questions to a cited, exportable manuscript. Use when the user wants to write a paper, article, thesis chapter, or literature review backed by Noesis evidence. Creates a persistent research project with explicit questions, success criteria, scope, and budget, picks the paper type (systematic review, scoping/narrative review, empirical/secondary-data study, position paper), routes each phase to the right skill (science-overview, systematic-review, ai-education-evidence, paper-draft), and tracks the work in a bounded Deep Research or Creation intake session. Drives the noesis-knowledge-engine MCP.
---

# Paper project

A paper in Noesis is three linked, owner-scoped objects, never a loose text file:

1. a **research project** (`create_research_project`) — the questions, success
   criteria, scope and budget the paper commits to;
2. an **evidence corpus** in one research namespace — ingested papers, claims,
   statistics, each with provenance;
3. an **authored report** (contract `noesis-authored-report-v1`) — the
   manuscript, where every sourced sentence points at that evidence.

This skill sets up (1), routes the work for (2), and hands off to
**paper-draft** for (3). Tools live on the **`noesis-knowledge-engine`** MCP
server (`mcp__noesis-knowledge-engine__*`). If that server is not connected,
say so and stop — do not draft the paper from model memory instead.

## Inputs to settle with the user first

Ask for anything missing; these decide everything downstream.

- **Working title and 1–3 research questions.** Questions, not topics:
  "Do LLM-based tutors improve secondary-school maths outcomes compared with
  business-as-usual instruction?" — not "AI in education".
- **Paper type** (table below).
- **Target venue / audience** — journal, conference, policy brief, thesis.
  Sets length, citation style and locale (`en-US`, `en-GB`, `de-DE`).
- **Time window and population** — publication years, education level
  (ISCED 0–8), regions/languages.
- **Reference manager** — a Zotero library id (user or group) if they have one.
- **Budget** — tokens / requests / `usd_micros` the project may spend.

| Paper type | Evidence route | Rigor gate |
|---|---|---|
| Systematic review / meta-analysis | **systematic-review** (registered protocol, dual screening, span-anchored extraction) | PRISMA counts exported from `export_systematic_review` |
| Scoping or narrative review | **science-overview** + **ai-education-evidence** | coverage & contradictions reported, not smoothed |
| Empirical / secondary-data study | **ai-education-evidence** (education statistics as of a vintage) + **science-overview** for related work | every number cites source, vintage, unit, period |
| Position / perspective paper | **science-overview** for the state of the art; commentary is allowed but labelled | each argument's premises are sourced |

## Workflow

1. **Namespace.** Use one research namespace for the paper (e.g.
   `paper-ai-education-2026`) so its corpus does not mix with other work.
   Confirm the principal holds `namespace:<ns>:write`.
2. **Create the project.**
   ```
   create_research_project(
     namespace="paper-ai-education-2026",
     request_key="paper-ai-edu-v1",          # stable: replays return the same project
     questions=["Do LLM-based tutors improve ... ?", "..."],
     success_criteria=[
       "Every sourced sentence cites an ingested, retained source",
       "Search strategy and inclusion criteria are reported reproducibly",
       "Coverage gaps and contradictory findings are stated explicitly"],
     scope={"domains": ["research"], "namespaces": ["paper-ai-education-2026"]},
     budget={"tokens": 2000000, "requests": 5000, "usd_micros": 50000000})
   ```
   `scope` must contain exactly `domains` and `namespaces`; budget keys are
   nonnegative integers. Revise later with `revise_research_project`; branch an
   alternative framing with `branch_research_project` rather than overwriting.
3. **Open a bounded session.** Evidence gathering is Deep Research; drafting is
   Creation. Use the **intake-mode** skill
   (`start_intake_mode(mode="Deep Research", ...)`), passing the project as a
   reference, so time spent and evidence recorded are auditable.
4. **Gather evidence** with the skill the paper type names. Record each
   evidence batch against the intake session (`command_intake_mode`
   `action="record"`) as references — never copied text.
5. **Pin what the paper is written against.** Before drafting, begin a
   research snapshot over the paper's namespace so the manuscript cites one
   consistent state of the corpus:
   `begin_research_snapshot(selection={"namespaces": ["paper-ai-education-2026"]})`.
   Its namespace generations become the report's `snapshot.generations`.
6. **Draft, cite, export** with **paper-draft**. Alongside it:
   **research-gaps** for the motivation and research agenda,
   **paper-figures** for the PRISMA diagram, evidence map and tables,
   **literature-watch** on a fixed rhythm until the search cut-off, and
   **reference-integrity** before anything is submitted or circulated.
7. **Close out.** Record spend with `record_research_project_expenditure`,
   complete the intake session, and close the snapshot
   (`close_research_snapshot`) once the export is done.

## Rules

- **No unsourced findings.** A statement about what studies show must trace to
  an ingested source; if Noesis cannot back it, the paper says the evidence is
  missing. Model background knowledge is not a citation.
- **Partial coverage is a result.** If a database the field relies on is not a
  Noesis connector (for education research: ERIC, see
  **ai-education-evidence**), the paper's methods and limitations say so.
- **Idempotent keys.** Reuse a `request_key` only to replay the same request;
  a new framing gets a new key.
- **Do not ingest, publish or submit without the user's go-ahead.** Harvesting
  external sources and exporting the manuscript are explicit user decisions.
