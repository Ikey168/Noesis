---
name: systematic-review
description: Design and run a protocol-registered systematic (or scoping) literature review in Noesis — register eligibility criteria, databases, search expressions, date window, two independent reviewers and extraction fields before any screening; define the evidence-qualifier fields; amend visibly; and export the PRISMA-mapped result (counts, study fields, ASReview CSV). The search and the screening/extraction stages run through the literature-search and screening-extraction skills. Use when a paper's claims rest on a systematic search of the literature, e.g. a review or meta-analysis on AI in education. Drives the noesis-knowledge-engine MCP review_protocol / review_candidate tools.
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

## 2. Search, screen, extract

The protocol is carried out by two stage skills:

- **literature-search**: per-database query plan, harvest, search-run
  receipts, ingestion, searches outside Noesis (ERIC), full texts, and
  `add_review_candidate`.
- **screening-extraction**: calibration, blinded dual screening,
  adjudication, span-anchored extraction and second-reviewer checks.

Check the protocol's `databases` against the connectors listed in
**literature-search** before registering it. A database without a connector
is searched outside Noesis and named as such in the protocol.

## 3. Qualify the evidence

Make these protocol `fields` so they are extracted and second-reviewed like any
other value; they decide how strong the paper's conclusions may be:

- **Design**: RCT, quasi-experimental, pre/post without control,
  correlational, qualitative.
- **Outcome type**: measured learning vs. perception, engagement or usage.
  Satisfaction is not a learning outcome.
- **Duration**: short interventions confound novelty effects.
- **Context**: country, education level (ISCED), subject, language.
- **Era**: a technology break changes what transfers. For AI in education,
  the ChatGPT release (30 Nov 2022) separates earlier tutoring-system evidence
  from LLM-tutor evidence.
- **Conflicts of interest**: authored or funded by the vendor of the
  system evaluated.

Accuracy figures for contested tools (for example AI-text detectors) are cited
from the specific evaluation, never from a vendor claim alone.

## 4. Export for the paper

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
