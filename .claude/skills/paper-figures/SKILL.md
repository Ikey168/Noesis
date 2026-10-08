---
name: paper-figures
description: Build the figures and tables of a Noesis paper from its exported evidence, never from hand-copied numbers — a PRISMA 2020 flow diagram derived stage by stage from the systematic-review export, an evidence map (e.g. AI-in-education sub-area × education level × study design) from reviewed study fields, a study-characteristics table, and context charts of education statistics that carry source, vintage, unit and period. Every figure ships with its data file and a provenance note, and enters the manuscript as an artifact dependency. Use when a paper needs figures, when review counts change, or when a reviewer asks where a number came from. Uses the systematic-review, education-statistics and authored-report exports plus the dataviz skill for chart design.
---

# Paper figures

Each figure is a function of an export: export → data file → figure → report
dependency. If the evidence changes, rerun the chain; never edit a number in a
figure by hand. Load the **dataviz** skill before writing chart code; it sets
form, colour and accessibility. `plotly` is in the repo's requirements; use the
venue's required tool if it has one.

Write figures, data files and scripts to the paper's working directory (ask the
user where), not into the Noesis repository: they are generated outputs.

## 1. PRISMA 2020 flow diagram

Source: `export_systematic_review(namespace, protocol_id)` (see
**systematic-review**). Do **not** draw it from the export's `counts` map: that
map merges title/abstract and full-text exclusions into one `exclude` status.
Derive each box from `candidates[]`:

| PRISMA box | Derivation |
|---|---|
| Records identified, per database | candidates grouped by `search_run_id`, mapped to the database each run searched |
| Duplicates removed | candidates minus distinct `(source_namespace, publication_id)` |
| Records screened | distinct publications |
| Records excluded | `screening.title_abstract.status == "exclude"` |
| Reports sought for retrieval | title/abstract `include` |
| Reports not retrieved | title/abstract `include` and `full_text_available == false` |
| Reports assessed for eligibility | title/abstract `include` with full text |
| Reports excluded, with reasons | `screening.full_text.status == "exclude"`, grouped by the exclusion criterion named in `decisions[].reason` |
| Studies included / reports of included studies | distinct `study_id` / count of full-text `include` |
| Records identified via other methods | searches outside Noesis (e.g. ERIC), a separate column from the protocol's records |

Candidates with status `pending` or `disputed` at either stage are shown in
their own box ("awaiting screening" or "awaiting adjudication"). They are never
folded into include or exclude. A diagram with open boxes is a draft; resolve
them before submission. To make the reasons groupable, ask reviewers to start
each exclusion reason with the criterion's label from the protocol.

Check that the boxes add up (identified − duplicates = screened; screened −
excluded = sought; and so on) and stop if they don't.

## 2. Evidence map

From the same export, use only fields with `review_state == "accepted"` (a second
reviewer accepted the value; `candidates[].fields`). For AI in education a useful default
is a grid of sub-area (tutoring, LLM tutors, assessment & feedback, learning
analytics, integrity, teacher use) × education level (ISCED), with cell size
= number of studies and colour or split = design (controlled vs. pre/post vs.
qualitative). Empty cells are a finding: cross-check them with
**research-gaps** before the text calls them gaps. Show the number of studies
with each field unextracted as "not reported", never as zero.

## 3. Study-characteristics table

One row per `study_id`: design, country, ISCED level, subject, sample size, AI
system, comparison, outcome measure, follow-up. Values come from accepted
fields; unaccepted or missing values print as "not reported" or "pending
review". This is the paper's supplementary table and a reviewer's first check.

## 4. Context statistics charts

From `country_education_statistics_as_of` / `institution_statistics_as_of`
(see **paper-draft**, "Citing statistics and AI systems"). Each series is labelled with source, indicator,
unit (and currency and scale as published), reference period and release
vintage. Keep different sources as separate series; no averaging, merging or
currency conversion. Show the publisher's special codes (missing, not
applicable, confidential) as gaps with a legend entry, not as zero, and mark
breaks in series from the comparability notes.

## 5. Provenance and the manuscript

For every figure and table save, next to the image:

- the data file (CSV) the figure is drawn from,
- the export it was derived from: contract, `sha256` where the export has one,
  protocol or series ids, revision or vintage, and export time,
- the script that produced it.

In **paper-draft**, the sentence that presents the figure ("Figure 1 shows…")
and any number quoted from it are `sourced` assertions with an `artifact`
dependency whose `id` is the data file's SHA-256 and whose `revision` is the
export it came from. Authored reports hold text, not images, so the figure
files travel with the manuscript, not inside the report.

## Rules

- No number in a figure that the export does not contain or that a recorded
  calculation does not derive from it.
- Pending, disputed, missing and suppressed values are shown, not dropped.
- A forest plot or pooled estimate only if the paper actually runs a
  meta-analysis with reviewed effect sizes; extracted effect sizes alone do
  not justify pooling.
- Regenerate all figures after any change to screening, extraction or cited
  vintages, and update the dependency hashes.
