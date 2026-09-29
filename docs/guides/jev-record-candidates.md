# Optional methodology and identity suggestions

`suggest_jev_methodology_record` accepts a registered methodology statement
ID. The adapter resolves the current study revision and requires the statement
to occur once, verbatim, in the current authorized document revision. It sends
that exact passage to the hosted decision runtime and returns suggested study
design, methodological family, and limitation categories. The result links the
statement, study revision, document revision, and character range. It does not
generate numeric values, sample sizes, effects, or new quotes. A missing or
ambiguous source passage is unavailable for this task. A category result is
published as `suggested` only when every requested field has a recognized
answer and the task is outside shadow mode; missing or unrecognized answers
produce `unavailable` with null categories.

`suggest_jev_entity_identity` and `suggest_jev_source_identity` accept one
existing reference ID and one to ten existing candidate IDs. The current
entity or source records are captured as versioned inputs; source snapshots
include reviewed aliases, localized names, registered identifiers, and the
identity revision. Changes to those records invalidate inspection and replay.
Choices include each supplied candidate, none, and uncertain. These
suggestions do not merge entities or prove publisher ownership, source
independence, reliability, or corroboration.

For all three tools, pass `namespace`, a unique `run_id`, a pinned hosted
`policy`, `max_cost_usd_micros`, and `network_approved=true` when a remote
request is intended. For example:

```json
{"namespace":"research","reference_id":"source-identity:<id>","candidate_ids":["source-identity:<other-id>"],"run_id":"alias-pilot-001","policy":{"hosted_allowed":true,"model":"jev-1.13.0","rubric_id":"source-identity-v1","policy_id":"pilot-v1","credential_ref":"typesafe","budget_id":"identity-pilot","max_total_cost_usd_micros":10000},"max_cost_usd_micros":100,"network_approved":true}
```

The example is the argument object for `suggest_jev_source_identity`.
No accepted identity changes are made by suggestion tools. A reviewer can
explicitly confirm a source alias with `accept_jev_source_alias_review`, which
uses the existing authorized source-alias decision method. A reviewer can
queue a confirmed entity merge proposal with `queue_jev_entity_merge_review`;
it remains pending until the existing admin correction review approves it.
Both review tools recheck the retained run and current record versions.

`evaluate_identity_candidates` and `evaluate_methodology_categories` in
`src/evaluation/jev_record_candidates.py` score independently labeled cases.
Identity evaluation reports false merges by homonym, alias, affiliation,
similar-name, multilingual-name, syndication, rebrand, and shared-domain
challenge.
Methodology evaluation reports categorical accuracy and abstention by field.
Fixture tests verify the plumbing; no independent benchmark or live provider
result is claimed, and automatic merge or category acceptance remains off.
