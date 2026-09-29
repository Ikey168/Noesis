# Optional Jev NLP suggestions

`src.argument_mining.jev_nlp_adapters` provides four explicit, source-bound
tasks over the authorized `DecisionRuntime`. They do not change the existing
claim detector, fact-check scheduler, sentiment pipeline or actor extractor.
Each task uses an exact revision and a verified character slice, returns its
durable model/rubric/source receipt, and marks the output `accepted: false`.
The knowledge-engine MCP exposes the same paths as `suggest_jev_claim_presence`,
`suggest_jev_checkworthiness`, `suggest_jev_sentiment`, and
`suggest_jev_attribution`; each requires `knowledge:decision:execute`, writes
only a durable decision receipt, and requires explicit `network_approved` for
remote execution. Their outputs follow the matching
`noesis-jev-*-suggestion-v1` JSON Schemas.
The existing surfaces expose opt-in entry points:
`src.argument_mining.models.suggest_claim_with_jev`,
`src.argument_mining.factcheck_scheduler.suggest_checkworthiness_with_jev`,
`src.nlp.sentiment_pipeline.suggest_sentiment_with_jev`, and
`src.argument_mining.attribution.suggest_speaker_with_jev`.

## Factual claims (#1642)

`suggest_claim_presence` uses the existing sentence boundaries and records
the original text, index and character span with bounded context. Noul's
`p_claim` is the probability of claim presence. `p_nonclaim` is exactly
`1 - p_claim`; selected class confidence is the probability of the selected
class and is recorded separately. A model/rubric-pinned validation policy
fits positive and negative thresholds, leaving an abstention region. Without
that policy the adapter does not select a class.

`evaluate_claim_presence` reports precision, recall and F1 by positive and
negative class, source content type, language and case kind. The six existing
content types and long, quoted, negated and transcript cases belong in the
held-out set. The historical local claim F1 in the benchmark report is a
baseline, not a Jev result.

## Check-worthiness (#1643)

`suggest_checkworthiness` accepts only an already detected claim ID and exact
claim span. It records separate impact and testability rubric scores, plus a
versioned weighted priority suggestion. It never deletes a factual claim,
changes truth status or mutates the fact-check scheduler. Unavailable scores
remain abstentions. `evaluate_checkworthiness` compares Jev and the existing
priority order under the same fixed checking budget using human priority
labels.

## Sentiment (#1644)

`suggest_sentiment` asks a target-specific Choice with positive, negative,
neutral and mixed labels. It preserves the full raw distribution and maps a
selected label to the current uppercase `label` / selected `score` / `text`
shape only with a frozen validation policy. It does not infer topic stance or
source trust and does not update trend aggregates. The evaluator stratifies
neutral, mixed, quoted and sarcastic cases by language and content type.

## Attribution (#1645)

`suggest_attribution` accepts only existing extracted candidate IDs, names
and roles, plus `none` and `uncertain`. It retains the statement locator,
context locator, original byline and authors. An uncertain response abstains;
the model cannot invent a candidate or change extraction metadata. A calibrated
`none` selection is returned as `suggested_choice: "none"` with no actor;
`uncertain` remains an abstention. Selected actors also require a frozen
validation policy. The evaluator reports selection precision and coverage for
nested quotations, reported speech, transcripts and institutional aliases.

The current offline tests use `fixture` labels. No independently labeled Jev
held-out corpus or measured gain is available; all evaluation helpers return
`task_ready: false` and production defaults remain local.
