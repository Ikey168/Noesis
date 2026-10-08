---
name: screening-extraction
description: Run the screening and data-extraction stage of a Noesis systematic review with two independent human reviewers — roles and scopes, a calibration round with an agreement statistic, blinded title/abstract and full-text screening with criterion-labelled reasons, optional ASReview prioritisation with a pre-declared stopping rule, adjudication of disagreements, data extraction anchored to exact character spans of committed full texts, second-reviewer checks, and risk-of-bias judgements. The agent prepares, finds passages and records what reviewers decide; it never makes a screening or extraction decision itself. Use after literature-search has added candidates and before the export feeds paper-figures and paper-draft. Drives the noesis-knowledge-engine MCP review_candidate and study_field tools.
---

# Screening and extraction

The stage where a systematic review earns the word "systematic": two people
decide independently, disagreements are resolved on the record, and every
extracted number points at the sentence it came from. Candidates come from
**literature-search** under the protocol from **systematic-review**. Tools are
on the **`noesis-knowledge-engine`** MCP server.

## 0. Roles, access and blinding

- **Reviewers** are the protocol's `reviewers`, each a distinct principal with
  their own Noesis session. Screening needs `knowledge:reviews:read`,
  `knowledge:reviews:write` and `namespace:<ns>:write`. Extraction additionally needs
  `document:<publication_id>:read` for each paper (or operator access).
- **Blinding.** `inspect_review_candidate` and `list_review_candidates` hide
  the other reviewer's decisions. `export_systematic_review` does not: it is
  owner-only and shows every decision. If the owner also screens, do not
  export until both reviewers have finished the stage, or let a non-screening
  coordinator own the protocol.
- **What the agent may do.** Show a candidate's title, abstract or full text
  next to the criteria, find passages, flag likely duplicates, and record a
  decision **the reviewer has stated for that candidate**. It never records
  a decision the reviewer did not make, never "pre-fills" a batch, and never
  acts as a second reviewer. The call is recorded under the session's
  principal, so it must be that person's decision.

## 1. Calibrate first

Before the main run, both reviewers screen the same random sample (about 50
candidates) at the title/abstract stage. Then:

- Compute agreement (Cohen's kappa, or percentage agreement for very small
  samples) from the two decision sets. Noesis does not compute it; it is a
  **calculation** in the paper, with the sample and the decisions as inputs.
- Discuss every disagreement. Where a criterion was ambiguous, clarify it with
  `amend_review_protocol(..., rationale=...)`. The amendment is visible, and
  earlier decisions keep the criteria they were made under.
- Repeat on a fresh sample if agreement is poor, and report both rounds.

## 2. Title/abstract screening

- `list_review_candidates(namespace, protocol_id, limit=50, offset=...)` pages
  through the candidates. `inspect_review_candidate` returns one with its
  current revision.
- Each reviewer, independently:
  `screen_review_candidate(namespace, candidate_id, stage="title_abstract", expected_revision=..., decision="include"|"exclude"|"pending", reason=...)`.
- **Start every exclusion reason with the protocol criterion it applies**
  ("E2: outcome is perception only"), so **paper-figures** can group exclusion
  reasons for PRISMA.
- When in doubt, include. Title/abstract screening is for removing clear
  exclusions; uncertain records go to full text.
- **Prioritised screening (optional).** The export's `asreview_unlabeled_csv`
  can drive ASReview's active-learning order. If you stop before screening
  everything, declare the stopping rule before starting (for example, stop
  after a fixed number of consecutive irrelevant records, plus a minimum share
  screened), record it as a protocol amendment, and report it. Model
  suggestions only set the order; they are never decisions.

## 3. Full-text screening

- Only candidates with `full_text_available=True` can be included or excluded
  at the full-text stage. Others stay `pending` and are reported as "not
  retrieved". See **literature-search**, step 4, for adding full-text
  candidates.
- The stage requires a title/abstract `include` on the same candidate.
- Each reviewer: `screen_review_candidate(..., stage="full_text", ...)`, with
  criterion-labelled reasons as above.

## 4. Resolve disagreements

`inspect_review_candidate` shows status `disputed` once both reviewers have
decided differently. Resolve by discussion, or with a third person, then
record it with `adjudicate_review_candidate(namespace, candidate_id, stage, screening_hash, decision, reason)`.
The `screening_hash` pins the exact decision set being resolved. If a
reviewer changes their decision afterwards, the adjudication no longer applies
and must be redone. Never resolve a disagreement by editing one side.

## 5. Data extraction

For each included study, fill the protocol `fields` from the committed full
text:

1. Find the passage. The agent can search the full text and show candidate
   spans. The reviewer confirms which one supports the value.
2. Reviewer A records it:
   `extract_review_field(namespace, candidate_id, field_name, value, start, end)`.
   `[start, end)` is the exact character span in the pinned source revision.
   The call fails if the revision is not committed or the span is out of
   range.
3. Reviewer B checks it against the span:
   `review_study_field(namespace, field_id, expected_revision, decision="accepted"|"rejected", reason)`.
   The same person cannot do both. Rejected values are re-extracted, not
   overwritten.

What to extract, and what not to:

- **Reported numbers, not derived ones.** Extract the means, SDs, n, test
  statistics or reported effect sizes as written. An effect size you compute
  from them is a **calculation** in the analysis, citing the extracted fields.
- **Missing is a value.** If a study does not report a field, record that as
  the outcome of extraction (for example `"not reported"` anchored to the
  methods section), rather than leaving it blank.
- **Risk of bias.** Noesis has no risk-of-bias instrument. Make each domain of
  the tool you use a protocol field (RoB 2 for randomised trials, ROBINS-I for
  non-randomised studies; for example `rob2_randomisation`). Each judgement is
  extracted with the span that justifies it and second-reviewed like any other
  field. Overall judgements follow the tool's own algorithm, applied in the
  analysis.
- The evidence-qualifier fields (design, outcome type, duration, context, era,
  conflicts of interest) come from the protocol; see **systematic-review**,
  section 3.

## 6. Hand-off

Once every candidate is resolved at both stages and every field reviewed, the
owner runs `export_systematic_review` (**systematic-review**, section 4). It
feeds **paper-figures** (PRISMA, evidence map, study table), **research-gaps**
and **paper-draft**. Record the stage's completion against the paper's intake
session (`command_intake_mode`, `action="record"`).

## Rules

- Decisions belong to the named reviewers; the agent records, never decides.
- Pending, disputed and not-retrieved candidates are reported, never dropped.
- Calibration rounds, stopping rules and amendments go into the methods.
