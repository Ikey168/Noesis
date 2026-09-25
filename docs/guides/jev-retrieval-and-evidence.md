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

All current adapter tests use `fixture` labels. No independent human Jev
evaluation or measured gain is available, so the evaluation helpers return
`task_ready: false` and no production selection is enabled.
