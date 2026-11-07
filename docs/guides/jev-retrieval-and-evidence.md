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
2. **Review.** Each proposed `contradicts` link goes to
   `suggest_contradiction_kind`, and each `supports` or `duplicate` link goes
   to `suggest_claim_relation`. This step runs outside the write
   transaction, because the decision runtime commits its own budget and
   receipt records. A pair planned from both sides is asked about once.
3. **Write.** Links, assessments and the scan ledger are written in one
   transaction.

| Mode | Effect on `claim_links` |
|---|---|
| `shadow` | Every planned link is written; the Jev assessment is recorded beside it |
| `gate` | A link Jev answers *against* is withheld, together with any temporal transition derived from it |

Jev can only withhold a planned link. It never adds a link or changes a
relation. A claim without exactly one occurrence in its document's current
committed revision (`unresolved`), a provider, policy or budget failure
(`unavailable`), or an abstention keeps the local link.

Every review is stored in `claim_link_jev_assessments` under the linking
`run_id`, so `delete_run` reverts it along with the links. The counts are in
`summary["jev"]`.

`document_revision_resolver(conn)` locates each claim's exact span, and only
those spans are sent to the provider. `build_jev_reviewer` wires the real
TypeSafe client to `TYPESAFE_API_KEY` (`credential_ref: "typesafe"`). The
daily run is opt-in:

```bash
python3 scripts/kb_brief.py --harvest --jev-links shadow --jev-policy policy.json
```

Shadow mode is the intended starting point. Gate mode should wait until a
human-labeled evaluation supports it.

All current adapter tests use `fixture` labels. No independent human Jev
evaluation or measured gain is available, so the evaluation helpers return
`task_ready: false` and no production selection is enabled.
