---
name: ai-education-evidence
description: Gather and qualify evidence for research on AI in education in Noesis — scholarly literature on intelligent tutoring, LLM/generative-AI tutors, automated assessment and feedback, learning analytics, academic integrity and AI detection, teacher use and AI policy; country and institution education statistics (UNESCO UIS, OECD Education at a Glance, Eurostat R&D, US IPEDS, ETER) cited as of a release vintage; and model records (Hugging Face Hub, OpenML, Epoch AI) for the AI systems studies used. Use when a paper, review or brief on AI in education needs a sourced corpus, context statistics, or a check of how strong the field's evidence is. Drives the noesis-knowledge-engine MCP education-statistics and ai-models tools plus the scholarly connectors.
---

# Evidence for AI-in-education research

Three evidence streams, kept apart and each cited on its own terms. Run inside
a **paper-project** namespace; tools are on the **`noesis-knowledge-engine`**
MCP server.

## 1. Literature

Harvest with the scholarly connectors and Deep Research pass described in
**science-overview** (or, for a systematic review, under the protocol in
**systematic-review**). Search per sub-area, not one broad "AI in education"
query — the sub-areas have different vocabularies and evidence quality:

| Sub-area | Query vocabulary (combine with learner/outcome terms) |
|---|---|
| Intelligent tutoring systems | "intelligent tutoring system", ITS, "cognitive tutor", "adaptive learning" |
| Generative-AI / LLM tutors | "large language model", ChatGPT, GPT-4, "generative AI", "AI tutor", chatbot |
| Automated assessment & feedback | "automated essay scoring", "automated feedback", "automated short answer grading" |
| Learning analytics / prediction | "learning analytics", "educational data mining", "dropout prediction", "early warning" |
| Academic integrity | "AI detection", "AI-generated text detection", plagiarism, "academic integrity" |
| Teachers & workload | "teacher" AND ("lesson planning" OR workload OR "professional development") AND AI |
| Equity, ethics & policy | "algorithmic bias" AND education, "AI literacy", "AI policy" AND (school OR university) |

Connector notes for this field:

- `openalex`, `crossref`, `semantic_scholar`, `doaj`, `core` cover most
  education journals; `dblp` covers AIED, EDM, LAK and L@S proceedings;
  `hal` and `zenodo` add European grey literature and datasets.
- **ERIC is not a Noesis connector.** It is the field's core database. If the
  paper claims a comprehensive search, search ERIC outside Noesis, record the
  date and query in the methods, and ingest the hits by DOI/URL so they become
  cited documents. Otherwise state the gap in the limitations.
- Policy documents (UNESCO, OECD, national ministries) are sources, not
  findings: ingest them by URL and cite them for what they recommend, not as
  evidence of effects.

## 2. Education statistics (context, never effects)

For enrolment, spending, teacher numbers, R&D and similar context. Acquire
through the source pack `primary-scientific-evidence` (sources
`unesco-uis-education-indicators`, `oecd-eag-education-indicators`,
`eurostat-rd-statistics`, `ipeds-institution-statistics`,
`eter-institution-statistics`) with `preflight_source_pack_run` then
`run_source_pack_execution` — live network only with the user's approval.

- `education_statistics_readiness()` — is the feature selected, which releases
  are held. Treat providers marked `unverified-live` as such in the paper.
- `country_education_statistics_as_of(namespace, country, as_of=..., indicator=..., isced=...)`
  and `institution_statistics_as_of(...)` — values as published in the vintage
  current at `as_of`, with definition, unit, period and comparability notes.
- `education_value_vintages(namespace, series_id, period)` — every provisional,
  final and revised value; cite the vintage you used.

The feature refuses rankings, composite scores, merged or averaged sources,
currency conversion and per-student ratios. If the paper needs a derived figure,
compute it in the paper's analysis, label it a calculation, and cite both inputs.

## 3. AI system records

When studies name the model they used (GPT-4, Llama, a fine-tuned tutor), pin
which system that was: `ai_model_records_as_of(namespace, subject, as_of=<study date>)`
and `ai_model_revision_history(...)` from the Hub, OpenML and Epoch AI records
(pack `technology-ai-models`). These give dated, cited facts about the model.
They do not give capability, safety or quality verdicts — do not present them as
such.

## Qualifying the evidence

Report these for the corpus; they decide how strong the paper's conclusions may be:

- **Study design** — RCT, quasi-experimental, pre/post without control,
  correlational, qualitative, design study. Many AI-in-education papers are
  small, short pre/post studies; say how many.
- **Outcome type** — measured learning vs. perception, engagement or usage.
  Self-reported satisfaction is not a learning outcome.
- **Novelty and duration** — short interventions confound novelty effects.
- **Context** — country, ISCED level, subject, language; note WEIRD-sample skew.
- **Era** — the ChatGPT release (30 Nov 2022) is a break: pre-2023 ITS evidence
  does not transfer automatically to LLM tutors, and the reverse.
- **Contradictions** — use `kb/{domain}/contradictions` and claim
  SUPPORTS/CONTRADICTS structure; disagreement is a finding to report.
- **AI-detection claims** — detector accuracy figures vary widely and are
  disputed, with documented bias against non-native writers; cite the specific
  evaluation, never a vendor claim alone.
- **Industry-authored evidence** — flag studies authored or funded by the vendor
  of the system evaluated.

## Rules

- Statistics describe context; they never show that AI caused anything.
- Every number carries source, vintage, unit and period in the paper.
- No unsourced background: if Noesis cannot back a statement about what the
  field shows, the paper reports the gap.
