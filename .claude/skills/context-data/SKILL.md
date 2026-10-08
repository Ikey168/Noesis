---
name: context-data
description: Gather the non-literature data a Noesis paper needs, cited as of a release — country and institution education statistics (UNESCO UIS, OECD Education at a Glance, Eurostat R&D, US IPEDS, ETER) with their definitions, units, periods, vintages and comparability notes, reviewable ROR identity for institutions, and records of the AI models and datasets that studies used (Hugging Face Hub, OpenML, Epoch AI) pinned to the study date. Covers deciding what to fetch from the included studies, licence acceptance and bounded source-pack runs, querying as of a date, linking records to the papers that cite them, evidence-bundle export, and how such data may and may not be used in the paper. Use when a paper needs context statistics, a secondary-data analysis, or a precise record of which AI system a study evaluated. Drives the noesis-knowledge-engine MCP education-statistics, ai-models and source-pack tools.
---

# Context data

Two kinds of data that are not papers but that a paper cites: **education
statistics** (the setting the studies took place in) and **AI system records**
(what the studies actually tested). Both come from source packs, are cited as
of a release, and carry no verdicts. Tools are on the
**`noesis-knowledge-engine`** MCP server; work in the **paper-project**
namespace.

## 1. Decide what to fetch

Start from the included studies (**screening-extraction** export), not from
what is available:

- the countries, education levels (ISCED) and years the studies cover, then
  the indicators that describe that context (enrolment, expenditure, teachers,
  digital access, R&D);
- the institutions, where a study names them;
- every AI system a study evaluated, with the study's dates.

Write that list down with the reason each item is needed. Statistics nobody
cites are clutter, and a paper that fetches everything invites fishing.

## 2. Readiness, terms and acquisition

- `education_statistics_readiness()` and `ai_models_readiness()` show whether
  the features are selected and which releases are already held.
- `education_source_contracts()` and `ai_models_source_contracts()` list each
  source's endpoint, licence, rate limits, identifiers, release vintages and
  bounded coverage. Read the licence before using a source. If a terms
  acceptance is needed, it is the user's decision:
  `accept_source_pack_license(pack_id, source_id, redistribution=False)`.
- Acquire with a bounded run, previewed first:

  ```
  request = {"pack_id": "primary-scientific-evidence", "run_key": "ctx-uis-2026-10",
             "operation": "search", "mode": "incremental",
             "source_ids": ["unesco-uis-education-indicators"],
             "parameters": {<as the source contract declares>}}
  preflight_source_pack_run(request)                         # credentials, terms, network policy
  run_source_pack_execution(request, live_network=True)      # only with the user's go-ahead
  ```

  Education sources: `unesco-uis-education-indicators`,
  `oecd-eag-education-indicators`, `eurostat-rd-statistics`,
  `ipeds-institution-statistics`, `eter-institution-statistics` (pack
  `primary-scientific-evidence`). AI sources: `huggingface-hub`, `openml`,
  `epoch-ai` (pack `technology-ai-models`).
- Every education provider is still marked `unverified-live`: no dated live
  check has been recorded yet. The paper says so wherever it uses one.

## 3. Education statistics

- Find series: `list_education_series(namespace, provider=..., concept=..., indicator=...)`.
- Country values as of a date:
  `country_education_statistics_as_of(namespace, country, as_of=<the paper's snapshot date>, indicator=..., isced=...)`.
  Each value comes with its definition, unit (currency and scale as
  published), reference period, release vintage, status and the publisher's
  special codes.
- Institution values:
  `institution_statistics_as_of(namespace, scheme="ipeds-unitid"|"eter-id", code=..., as_of=...)`,
  or `ror=...` once an identity match is confirmed (below).
- Revisions: `education_value_vintages(namespace, series_id, period)` lists
  every provisional, final and revised value. Cite the vintage you used, not
  just "UNESCO".

**Institution identity.** To ask by ROR id, keep ROR records
(`record_education_ror_records(namespace, ror_ids=[...])`), let Noesis propose
matches (`propose_education_ror_matches`), and have a person with review scope
accept or reject each candidate (`review_education_ror_match`, with a reason).
Published identifiers match exactly; name-and-country matches are candidates
until reviewed. Do not review your own inference.

**Linking to papers.** `link_education_record(namespace, subject, target, citation)`
links an institution to a scholarly work or funding record only when the
citation states the identifier. There are no name or keyword joins.

## 4. AI system records

For each system a study evaluated:

- `list_ai_model_records(namespace, source=...)` to find the record.
- `ai_model_records_as_of(namespace, subject, as_of=<the study's data-collection date>)`:
  each source's revision at that date (Hub sha, OpenML id and version, Epoch
  vintage) side by side, with self-reported results kept separate from OpenML
  evaluations and Epoch estimates.
- `ai_model_revision_history(namespace, subject)`: renames, gating, removals
  and licence changes, which matter when a study says only "GPT-4" or
  "ChatGPT".
- Cross-source identity: `list_ai_model_identity_matches`, then
  `review_ai_model_identity_match` (a person, with a reason).
  `link_ai_model_records` links records to papers and dataset DOIs by stated
  identifier.
- `export_ai_model_evidence(namespace, subject, as_of=...)` produces a portable
  evidence bundle for the supplement.

Closed models served only through an API (most chat assistants) often have no
Hub revision. Record what the study states (product name, date, interface) and
say the exact version is unverifiable, rather than guessing one.

## 5. Using the data in the paper

- **Context, not effects.** Statistics describe the setting. They never show
  that an AI system caused an outcome, and they are not mixed into an effect
  synthesis.
- **Cite completely.** Every number states source, indicator, unit,
  reference period and vintage, as a `source` dependency in **paper-draft**.
- **Nothing harmonised silently.** The features refuse rankings, composite
  scores, merged or averaged sources, currency conversion and per-student
  ratios. A derived figure you need is a `calculation` over cited inputs, with
  the method stated; different sources stay side by side.
- **No verdicts on models.** AI records carry no capability, safety, quality
  or openness judgement, and none may be implied from them.
- Charts: **paper-figures**, section 4. Keeping values current: monitors in
  **literature-watch**.

## Rules

- Fetch what the studies need, decided before looking at values.
- Ask before accepting licences or running live acquisition.
- Identity matches and links are reviewed by a person; inferred matches are
  candidates until then.
