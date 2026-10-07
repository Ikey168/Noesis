---
name: systematic-review
description: Run a protocol-registered systematic (or scoping) literature review in Noesis — eligibility criteria and search plan fixed up front, candidates traced to their search run and source revision, independent dual screening with blinded reviewers and adjudication, span-anchored data extraction with second-reviewer checks, visible protocol amendments, and a PRISMA-mapped export (counts, study fields, ASReview CSV). Use when a paper's claims rest on a systematic search of the literature, e.g. a review or meta-analysis on AI in education. Drives the noesis-knowledge-engine MCP review_protocol / review_candidate tools.
---

# Systematic review

Noesis keeps a systematic review as an auditable ledger: the protocol is
registered before screening, every candidate is traced to the search that found
it, reviewers decide independently, and extracted values are anchored to exact
character spans of a committed source. Tools live on the
**`noesis-knowledge-engine`** MCP server. Start from a **paper-project** so the
review belongs to a research project and namespace.

## 1. Register the protocol (before any screening)

```
create_review_protocol(namespace="paper-ai-education-2026", request_key="srp-v1", content={
  "question": "In K-12 and higher education, do generative-AI tutors improve learning outcomes compared with non-AI instruction?",
  "inclusion": ["Empirical study with a comparison condition",
                "Learners in ISCED 1-7 settings",
                "Intervention uses a generative or conversational AI system",
                "Reports a learning outcome (test score, grade, validated measure)"],
  "exclusion": ["Opinion, editorial or tool description without outcome data",
                "Outcome is only satisfaction, perception or usage",
                "Not in English or German"],
  "databases": ["openalex", "crossref", "semantic_scholar", "dblp", "doaj",
                "ERIC (searched outside Noesis; recorded manually)"],
  "search_expressions": ["(\"large language model\" OR ChatGPT OR \"generative AI\" OR \"AI tutor\") AND (student* OR learner*) AND (achievement OR \"learning outcome*\" OR performance)"],
  "date_from": "2022-11-30", "date_to": "2026-09-30",
  "reviewers": ["<principal-a>", "<principal-b>"],
  "fields": ["study_design", "country", "isced_level", "subject", "sample_size",
             "ai_system", "comparison", "outcome_measure", "effect_size", "risk_of_bias_notes"]})
```

- All nine keys are required; `date_from`/`date_to` are ordered ISO dates.
- **At least two distinct reviewers.** They must be real principals who will
  each screen; an agent may not pose as the second reviewer. If the user is
  working alone, say the review is single-screened and run it as a scoping
  review instead — do not fabricate independence.
- Changing criteria later: `amend_review_protocol(..., rationale=...)`. The
  amendment is visible in the export; earlier candidates keep their original
  criteria. Never quietly re-create the protocol.

## 2. Search and add candidates

Run each `search_expression` through the scholarly connectors with the
protocol's date window (see **science-overview**, step 2, for the connector
table and ingestion path). Ingest hits so each paper is a Noesis document, then
add each one:

```
add_review_candidate(namespace=..., protocol_id=..., protocol_revision=1,
  publication_id=<document id>, source_revision=<committed revision>,
  source_namespace="paper-ai-education-2026", search_run_id=<run receipt id>,
  study_id=<one id per study; reuse it for multiple reports of the same study>,
  title=..., abstract=..., full_text_available=<true only if the text is ingested>)
```

Dedupe by DOI before adding; group companion papers under one `study_id` so
study and publication counts stay distinct.

## 3. Screen independently

- Each reviewer: `screen_review_candidate(..., stage="title_abstract",
  decision="include"|"exclude"|"pending", reason=...)`, then `stage="full_text"`
  for includes. Reviewers cannot see each other's decisions
  (`inspect_review_candidate` / `list_review_candidates` are blinded).
- Missing full text stays `pending` — it cannot be included or excluded.
- Disagreements: `adjudicate_review_candidate(..., screening_hash=...)` against
  the exact decision set being resolved.
- An agent may **suggest** an ordering (the export's `asreview_unlabeled_csv`
  feeds ASReview) but never records a screening decision on a human's behalf.

## 4. Extract data

`extract_review_field(namespace, candidate_id, field_name, value, start, end)`
proposes a value for a protocol field, anchored to the exact `[start, end)`
span of the committed full text. A second reviewer accepts or rejects it with
`review_study_field` (`decision="accepted"|"rejected"`). Values without a span (e.g. an effect size you computed)
belong in the paper's analysis, labelled as calculations — not as extracted
fields.

## 5. Export for the paper

`export_systematic_review(namespace, protocol_id)` returns the protocol and
amendments, every candidate with screening and fields, distinct
candidate/publication/study counts, and a `prisma_reporting_map` from PRISMA
2020 items to the export fields. Use it for the PRISMA flow diagram and the
methods section, and hand the included studies to **paper-draft**.

## Rules

- The export's `limitations` are real: PRISMA mapping identifies reporting
  inputs, not methodological compliance; risk of bias and synthesis are separate
  work. Carry those limitations into the paper.
- Report searches done outside Noesis (ERIC, Google Scholar, hand searching) in
  the methods with their dates; they are not covered by Noesis provenance.
- Never skip, delete or overwrite a screening decision to make counts tidy.
