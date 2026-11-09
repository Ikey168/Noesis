# Optional Jev retrieval and evidence suggestions

`src.kb.jev_evidence_adapters` contains source-bound adapters for #1632–#1634.
Each caller must provide an exact source revision, principal/scopes, a hosted
policy and explicit remote authorization. `DecisionRuntime` captures and
rechecks each revision, transmits only the verified cited character slice,
and records its model/rubric/policy receipt. These adapters never mutate the
local reranker, answer policy, claim links or OSINT read views.

## Retrieved shortlist (#1632)

`suggest_shortlist_rerank` takes up to 20 already retrieved candidates. Every
candidate includes its original score/index, exact source reference and
character locator. It asks independent Noul relevance and answer-bearing
questions, then returns all candidates with suggested scores and original
metadata. Candidate indices and original scores are included in the durable
decision input. Any unanswered score or unavailable run preserves the entire
original order and shortlist. The existing
context assembler continues to apply source diversity, independence,
evidence rules, token budgets and citation selection. The adapter never
scores the corpus or marks a shortlist as accepted.

`evaluate_jev_reranking` compares fusion, MiniLM, Qwen and Jev on identical
held-out candidate IDs. It reports NDCG, answer-bearing recall, Jev coverage,
p95 latency and retry-inclusive cost. Missing latency or cost measurements
stay null. Fixture judgments require an explicit `allow_fixture=True` and do
not count as human evaluation.

## Cited answer support (#1633)

`suggest_answer_support` verifies that a citation's quote and start/end span
match the current captured revision before asking a Choice question about
entailment, contradiction, neutrality or unavailable evidence. The original
citation remains attached. The suggestion neither counts as an independent
source nor overrides refusal, structural answer validation or evidence
coverage. A missing/stale revision or changed quote fails before inference.
`src.kb.answer_support_eval.evaluate_support` remains the human-gold
evaluation hook for unsupported citations, contradictions, corrections,
incomplete coverage and appropriate refusal. The paired Jev/local-NLI
evaluator requires those case types, explicit citation state and coverage,
plus human relevance and refusal judgments; it reports false support and
contradiction rates without treating fixtures as acceptance evidence.

## Claim pairs (#1634)

`suggest_claim_relation` assesses both directions using exact claim spans.
Mutual entailment needs the existing high-similarity gate before a duplicate
suggestion; opposing entailment and contradiction abstain. Conflicting local
directional entailment/contradiction labels abstain before the hosted call;
long claim spans require a complete local-window coverage result. The duplicate
suggestion also rejects an identical source span. The deterministic
same-source temporal correction/retraction logic remains in
`src.kb.claim_links`, and the adapter does not write any graph edge. Existing
OSINT read views continue to consume stored edges without hosted calls.
`evaluate_jev_claim_relations` reports false support, contradiction and
duplicate rates against the local NLI baseline on the same held-out pairs.
It preserves `a_supports_b` versus `b_supports_a`, so a reversed support
direction is scored as false.

## Contradiction typing and corroboration checks

Local NLI and the conflict graph flag many pairs that are not genuine
contradictions. `suggest_contradiction_kind` takes two exact claim spans that
were already flagged and asks one Choice (`direct`, `quantitative`,
`temporal`, `scope`, `attribution`, `not_contradiction`, `unclear`) plus two
independent Noul questions: can both claims hold at once, and are they about
the same subject. The `assessment` is derived in code:

| Answers | Assessment |
|---|---|
| direct/quantitative, jointly-true below threshold, same subject | `conflict` |
| temporal, scope or attribution | `reconcilable` |
| not a contradiction and jointly true | `no_conflict` |
| unclear, unanswered, or a kind the Noul answers disagree with | abstain |

The caller's local label (`local_relation`) is echoed with an
`agrees_with_local` comparison but never sent to the provider. Thresholds are
uncalibrated defaults (`thresholds.calibrated: false`).

`suggest_corroboration` asks whether an exact evidence span supports,
partially supports, contradicts or is unrelated to an exact claim span, and
separately how independent the account reads. It is a check on an existing
`claim_evidence`/`claim_links` edge: `counts_as_independent_source` is always
false, and probable-origin lineage in `src.osint.independence` remains the
only authority for independence.

Neither adapter writes `claim_links`, `claim_conflicts` or `claim_evidence`.

## Local corroboration and contradiction rules

These deterministic rules apply with or without Jev:

- `src.kb.claim_links` abstains when one NLI direction entails and the other
  contradicts, counting the pair in `summary["abstained"]` instead of writing
  the more confident side.
- `src.argument_mining.conflict_graph.detect_conflict` needs an opposing
  signal: an explicit evidence contradiction, negation-aware opposite
  polarity, a negation mismatch, or different values for the same unit
  (`5 percent` vs `7 percent`). Different source formats alone are not a
  conflict. Each conflict lists the `signals` that fired.
- `src.osint.contradiction_scan` and the OSINT telemetry read the conflict
  graph's `direct`/`implied` types as well as `contradicts`, with direct
  conflicts ranked first.
- `src.osint.corroborate` also reads `claim_links`. A `supports` link counts
  only in its entailment direction (other claim to this one), a `duplicate`
  counts as support, and `contradicts` counts against. One document reached
  through several layers counts once per side, with `found_via` listing the
  layers. Same-source `corrects`/`retracts`/`supersedes` links show up as
  `revisions`, `corrected_by_source` and `retracted_by_source`, never as
  corroboration. A source found on both sides is listed in `mixed_sources`.

## Jev in the claim linking pass

`run_claim_linking_pass` and `run_cross_backing_link_pass` take an optional
`jev_reviewer` (`src.kb.claim_link_review.JevClaimLinkReviewer`). The pass
runs in three steps:

1. **Plan.** Local NLI proposes links. Nothing is written yet.
2. **Review.** Each proposed link is sent to the Jev task for its relation:

   | Relation | Task |
   |---|---|
   | `contradicts` | `suggest_contradiction_kind` |
   | `supports`, `duplicate` | `suggest_claim_relation` |
   | `supersedes`, `corrects`, `retracts` (from a newer same-source claim) | `suggest_supersession` |

   The supersession rubric has its own question: "does the later claim
   update, correct, retract or replace the earlier one?" It asks this
   instead of "do they conflict?", which rejected most genuine updates.
   This step runs outside the write transaction, because the decision
   runtime commits its own budget and receipt records.
3. **Write.** Links, assessments and the scan ledger are committed every
   `write_chunk_claims` claims (default 500). A single transaction for a
   whole corpus ran out of memory. If the pass crashes mid-write, later
   claims stay unscanned and the next pass picks them up.

**Each distinct question is asked once.**

- A pair planned from both sides is one link.
- A link whose claim texts repeat word for word elsewhere reuses that
  answer (`reused_from: identical_text`).
- Duplicates are reviewed first. Supports and contradicts links between two
  groups of Jev-confirmed duplicates reuse one answer
  (`reused_from: duplicate_group`).
- Each reused assessment records where its answer came from in
  `detail_json.reused_from`.

**Restarts reuse answers already paid for.** Reviews are asked in chunks
of `review_chunk_size` (default 200). Each answer is stored in
`claim_link_review_cache` as soon as its chunk returns. The cache is keyed
by reviewer identity (model, rubric, policy, batched relations and decision
rules), the claim pair and the claim texts. A restarted pass reuses these
answers (`reused_from: cache`). Provider failures are not cached, so they
are asked again. Pass `review_cache=False` to disable this.

**Progress.** The planned total per relation is logged before reviewing,
and progress is logged after each chunk. Both are reported in
`summary["jev"]["planned"]` and `summary["jev"]["reused"]`.

**Decision rules**, recorded in every assessment's `detail_json.decided_by`:

- `tie_break`: what to do when Jev's Choice label disagrees with its Noul
  probabilities. `abstain` (default) keeps the link, `label` trusts the
  label, and `probabilities` trusts the probabilities. This is set on
  `JevClaimLinkReviewer` or with `--jev-tie-break`. Re-asking does not help:
  per-link review repeats its verdict about 99.6% of the time.
- `same_subject_gate` (optional, `--jev-same-subject-gate`): a conflict
  Jev confirms is rejected (`different_subject`) when its same-subject
  probability is below this value. This is a cheap complement to the local
  [subject gate](claim-hygiene.md#2-subject-gate-for-contradictions).

**Budget.** Per-link reviews settle their budget reservation to the
reported input tokens (`input_price_usd_micros_per_million_tokens`,
default 42,000), as batched reviews do, so the budget ledger matches the
provider's usage.

| Mode | Effect on `claim_links` |
|---|---|
| `shadow` | Every planned link is written; the Jev assessment is recorded beside it |
| `gate` | A link Jev answers *against* is withheld, together with any temporal transition derived from it. A `supports` link Jev reads in the opposite direction is written reversed instead |

A shadow run's stored verdicts can be applied afterwards, with no new Jev
requests, using `scripts/apply_jev_link_verdicts.py`. Operator overrides for
Jev's mistakes are respected by both that script and gate mode. Before
linking, boilerplate claims are excluded, and a contradiction is only
proposed between claims about the same subject. See
[claim hygiene](claim-hygiene.md).

Jev can only withhold a planned link. It never adds a link or changes a
relation. Three outcomes keep the local link:

- `unresolved`: the claim text does not occur in its document's current
  committed revision at all. Spans are found, in order, from the offsets
  stored at extraction, an exact unique match, the first of several
  occurrences, or a whitespace- or quote-insensitive match. Which one was
  used is recorded in `detail_json.spans`.
- `unavailable`: a provider, policy or budget failure. A run that never
  answered, for example because 429s ran out of retries, is recorded as
  `unavailable` with its error code, not as an abstention.
- `abstained`: Jev answered without a usable relation. A `shadow` task
  rollout also abstains, with reason `shadow_rollout`.

Every review is stored in `claim_link_jev_assessments` under the linking
`run_id`, so `delete_run` reverts it along with the links. The counts are in
`summary["jev"]`, including `requests`, the number of hosted requests made.

`document_revision_resolver(conn)` locates each claim's exact span, and only
those spans are sent to the provider. `build_jev_reviewer` wires the real
TypeSafe client to `TYPESAFE_API_KEY` (`credential_ref: "typesafe"`). The
daily run is opt-in:

```bash
python3 scripts/kb_brief.py --harvest --jev-links shadow --jev-policy policy.json
```

Shadow mode is the intended starting point. Gate mode should wait until a
human-labeled evaluation supports it.

### Batched review (opt-in)

By default each link is reviewed in its own request, with its own hosted
decision run and budget reservation. System One answers every question in a
request in one parallel pass, so a batched reviewer packs many links into
each request instead. Pass a `src.kb.claim_link_batch.BatchConfig` as
`batch=` to `JevClaimLinkReviewer` or `build_jev_reviewer`, or use
`--jev-batch` on the daily run.

Batching is enabled per relation (`BatchConfig.relations`,
`--jev-batch-relations`). The default batches **duplicates only**. On a
240-link sample, the share of verdicts matching per-link review was:

| Relation | Batched vs per-link |
|---|---|
| Duplicates | 96–98% |
| Supports | 84–88% |

Each other relation is reviewed per link until its own validation gate
passes.

```bash
python3 scripts/kb_brief.py --harvest --jev-links shadow --jev-policy policy.json --jev-batch
```

Batched review applies the same verdict rules as per-link review
(`derive_claim_relation`, `derive_contradiction_assessment`, `link_agrees`)
and writes the same assessment rows. Each row's `detail_json.batch` records
`batch_id`, `batch_size` and `split_depth`, and its `decision_run_id` is the
batch run.

| Option | Default | Meaning |
|---|---|---|
| `state_token_budget` (`--jev-batch-state-tokens`) | 24,000 | Estimated tokens of request state, which holds the claim spans. Must stay under `question_token_limit` |
| `request_token_limit` | 60,000 | Estimated tokens of state plus all questions; at most 64,000 |
| `question_token_limit` | 32,000 | State plus the longest question; at most 32,000 |
| `max_links` (`--jev-batch-max-links`) | 120 | Links per request |
| `concurrency` (`--jev-batch-concurrency`) | 1 | Requests in flight. May not exceed the policy's `max_concurrent` (at most 4) |
| `max_attempts` | 3 | Attempts per request for 429/529, at most 3 |
| `bytes_per_token` | 3.0 | Packing estimate: UTF-8 bytes per token |
| `input_price_usd_micros_per_million_tokens` | 42,000 | $0.042 per million input tokens; output is free |
| `deadline_s` | 60 | Per-request deadline, retries included |

Provider limits for jev-1.13.0 and 1.14.0 are 64k tokens per request (state
plus all questions) and 32k tokens per question (state plus one question).
There is no limit on question count, and the body may be up to 32 MiB.

**Packing.** Links are packed greedily in order. A link is added only if the
estimated state stays within `state_token_budget`, state plus all questions
stays within `request_token_limit`, and state plus the longest question
stays within `question_token_limit`. The estimate is UTF-8 bytes divided by
`bytes_per_token`; 3.0 is conservative for English, where about 4 is
typical. A link that cannot fit in a request on its own is `unavailable`
with error `input_limit`.

**Isolation.** Each link's claim spans are stored in state as
`links.L0007.claim_a` and `claim_b`. Every question for that link has the
id prefix `L0007__`, and its instructions name `links.L0007`, so an answer
can only be attributed to its own link. Claims shared by several links are
repeated in each link entry rather than cross-referenced.

**Source binding.** Each distinct claim span is one exact committed
revision slice. The runtime authorizes it, rechecks it before every attempt
and before publication, and builds the request state itself from those
captures (`compose_state`). Caller text never reaches the wire. Spans that
change between capture and request make that batch `unavailable`
(`source_changed`).

**Budget.** Each request reserves a strict upper bound before it is sent:
every body byte is counted as a token, plus 32 tokens per question and 512
per request, priced at the input rate, times `max_attempts`. The runtime
refuses a request that would push the ledger past
`max_total_cost_usd_micros`; those links are `unavailable` with error
`budget_exhausted` and are written. Once answered, the reservation is
settled to the response's reported `usage.input_tokens`:

- reservations for attempts that were never made are released;
- failed attempts stay charged their full reservation;
- if reported cost ever exceeded the reservation, it is charged as reported
  and flagged `budget_overrun`.

The billing model is an assumption: state is billed once per request.
`docs.typesafe.ai` could not be checked when this was written. If the
provider bills state once per question, settlement records the real charge,
and the overrun flag shows the reservation was too low.

**Failures.**

- 429 and 529 are retried by `TypeSafeClient` with bounded exponential
  backoff (at most 8 s, honouring `Retry-After` within the deadline).
- `400 max_tokens_exceeded` splits the batch in half and retries each half.
  A single link that still exceeds it is `unavailable`.
- Any other failed batch makes only that batch's links `unavailable`, so
  they are written.
- With `response_retention: "none"`, error codes are not retained, so a
  batch can't be split and becomes `unavailable`.

**Validate before enabling.** Batching changes what the model sees: dozens
of independent pairs in one state instead of one. Compare the two paths on
already planned links before using `--jev-batch` in production:

```bash
python3 scripts/validate_jev_link_batching.py --policy policy.json --sample 480 --batch-sizes 25 120 --out batching-report.json
```

The script samples stored `supports`, `duplicate`, `contradicts` and
supersession links. It reviews the same links in shadow mode, without
writing links or assessments:

- per link **twice**, as a repeat baseline, so batching drift can be told
  apart from Jev's normal run-to-run variation;
- batched at about 25 and about 120 links per request.

It reports:

- verdict agreement by relation, over links both paths answered;
- outcome agreement (status, verdict and Jev relation);
- status and verdict confusion pairs;
- request counts, wall time and p50/p95 request latency;
- input tokens and settled cost.

Each batched comparison also shows the per-link repeat agreement next to
it (`repeat_baseline`). Each relation is judged separately, and
`recommended_relations` lists those that pass. A relation passes when it
has:

- at least `--min-links-per-relation` links answered by both paths
  (default 20);
- verdict agreement of at least `--min-agreement` (default 0.95);
- verdict agreement no more than `--max-drift` (default 0.02) below the
  per-link repeat.

Pass the passing relations to `--jev-batch-relations`. The report is advice
for an operator: it always says `production_switch: false`, and nothing
enables batching automatically.
The validation sends real claim spans to TypeSafe and spends from the
policy's budget.

All current adapter tests use `fixture` labels. No independent human Jev
evaluation or measured gain is available, so the evaluation helpers return
`task_ready: false` and no production selection is enabled.
