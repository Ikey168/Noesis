# Optional machine screening for systematic reviews

The `suggest_systematic_review_screening` tool assesses a registered candidate
against its versioned protocol. Set `stage` to `title_abstract` or `full_text`
and provide a unique `run_id`, pinned hosted `policy`, explicit
`network_approved=true`, and a positive `max_cost_usd_micros`. Hosted
execution is off unless these explicit controls and the current decision
authorization permit it. Use
`inspect_systematic_review_screening_suggestion` with the same namespace,
candidate, stage, and run ID to read the retained result.

Each protocol inclusion and exclusion criterion receives a stable code (`I01`,
`E01`, etc.) and a machine status: satisfied, not satisfied, not reported, or
abstained. A separate choice selects one supplied title, abstract, or document
window. Title and abstract fields must occur verbatim in the pinned document
revision before they are sent; each locator then identifies exact character
offsets in that revision. Unlocated metadata, missing abstracts and missing
full text return pending without a provider call. A model selected location is
not an independently verified quote. If a required span is missing, an answer
is unavailable, or the model cannot select a supporting span, advice remains
pending. Full-text suggestions require the existing title/abstract stage to
resolve to include through the normal independent reviewer process.

Full-text coverage is explicit. At most 65,536 characters in 2,048-character
windows are considered, and the hosted request binds the complete supplied
range. Longer or oversized source payloads return pending without criterion
decisions and show the missing coverage; the tool does not infer eligibility
from a partial document. Candidate, protocol, source revision, model, rubric,
policy, cost and remote-use provenance are retained. A protocol amendment or
source revision change marks an earlier suggestion stale. Suggestion rows are
stored separately from `systematic_review_screening` and never count as
reviewer votes, resolve disagreements, or create adjudications.

For a fixture candidate already admitted by `add_systematic_review_candidate`,
the call shape is:

```json
{"namespace":"review-a","candidate_id":"candidate:<id>","stage":"title_abstract","run_id":"pilot-001","policy":{"hosted_allowed":true,"model":"jev-1.13.0","rubric_id":"screen-v1","policy_id":"pilot-v1","credential_ref":"typesafe","budget_id":"review-pilot","max_total_cost_usd_micros":10000,"response_retention":"decision"},"max_cost_usd_micros":100,"network_approved":true}
```

`evaluate_screening_suggestions` in
`src/evaluation/systematic_review_screening.py` scores independently labeled
cases separately for each stage. It reports inclusion recall, false exclusions,
pending rate, and criterion accuracy. Fixture labels must be explicitly opted
into for plumbing checks; they do not establish measured performance or enable
automated exclusion. The evaluator reports `task_ready: false` until a stage
has independent human labels and a predeclared false-exclusion limit.
