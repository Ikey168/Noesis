# Optional Jev typed decisions

Noesis can execute a bounded TypeSafe Jev request against exact, currently authorized document or Awareness inbox versions. The provider is off unless the caller explicitly enables hosted processing and supplies a policy, budget and secret in the operator environment. A model result is a source-bound suggestion. Existing domain methods still own accepted decisions.

The shared execution path is `src/kb/decision_runtime.py`. CLI request kinds are `decision`, `decision_read`, `decision_task`, `decision_rollout`, `decision_rollout_read`, and `typed_decision_evaluation`; MCP tools are `run_hosted_typed_decision`, `inspect_hosted_typed_decision`, `configure_jev_task_rollout`, `inspect_jev_task_rollout`, `suggest_jev_task`, `suggest_awareness_with_jev`, `accept_awareness_jev_suggestion`, and `suggest_jev_epistemic_kind` on `noesis-knowledge-engine`.

## Execute an authorized request

Set `TYPESAFE_API_KEY` only in the operator environment. The request file must not contain the key. Grant the principal `knowledge:decision:execute`, `namespace:<namespace>:write`, and current read access to each exact source (`document:<id>:read` for documents, or the intake access applicable to its owner). A later inspection requires `knowledge:decision:read` and namespace read access. The `operator` scope follows the existing local trusted-operator convention.

Example request using a committed document revision:

```json
{
  "kind": "decision",
  "namespace": "research",
  "run_id": "relevance-2026-09-23-001",
  "task": "passage-relevance-v1",
  "network_approved": true,
  "sources": [{"document_id": "DOCUMENT_ID", "revision_id": "REVISION_ID"}],
  "state": {"question": "What changed in the procedure?"},
  "questions": {
    "relevant": {
      "type": "noul",
      "instructions": "Does the captured source passage directly address the research question?"
    }
  },
  "policy": {
    "hosted_allowed": true,
    "model": "jev-1.13.0",
    "rubric_id": "passage-relevance-v1",
    "policy_id": "pilot-policy-v1",
    "credential_ref": "typesafe",
    "budget_id": "research-pilot-001",
    "max_total_cost_usd_micros": 10000,
    "response_retention": "decision"
  },
  "max_attempts": 1,
  "max_cost_usd_micros": 100,
  "deadline_s": 20
}
```

Run it with `python -m src.noesis_cli.optional --config /path/to/noesis.toml --request /path/to/request.json`. The configured principal and trusted `NOESIS_OPTIONAL_SCOPES` provide authority; request JSON cannot grant itself scopes. An inspection request uses `{"kind":"decision_read","namespace":"research","run_id":"relevance-2026-09-23-001"}`. Replayed runs recheck current source access and version.

The runtime adds captured source text under `state.sources`; callers cannot supply that field. Inbox references use `{"item_id":"feed:...","source_version":2}` and the actual retained item version. User input references require a trusted server-side resolver. The service bounds source count, size, attempts, elapsed time, and reserved cost. A reserved but interrupted run is indeterminate and is never retried under the same run ID.

Choice answers preserve both `selected_probability` and `vendor_confidence`. They have different meanings. Noul is a yes probability and has no vendor confidence. Unavailable or abstained responses are not negative answers. Store the returned versioned model, rubric, policy and source binding with downstream suggestions. Calibrate each task before enabling accepted decisions.

## Pin a task rollout

An authorized operator can configure a task as `off`, `shadow`, or `suggestion`. A configured `off` task rejects hosted execution. `shadow` runs retain receipts for evaluation but return no domain action advice. `suggestion` requires a pinned model, rubric and evaluation evidence reference; configuring that reference records the operator's choice and does not itself prove the evaluation passed. Unconfigured tasks remain available only through explicit, authorized manual requests with `network_approved: true` and a bounded policy. The local classifier, retrieval and workflow defaults are unchanged.

For the CLI, submit `{"kind":"decision_rollout","namespace":"research","task":"jev-stance-v1","mode":"shadow","model":"jev-1.13.0","rubric_id":"stance-v1"}` with trusted `knowledge:decision:configure` and namespace write scopes. `decision_rollout_read` and `inspect_jev_task_rollout` return the current mode; an unconfigured task reports `off`. For `suggestion`, add `evaluation_ref` pointing to the reviewed held-out report. Rollback is another `decision_rollout` request with `mode: "off"`.

## Evaluate a task

Use frozen validation and held-out test JSON arrays containing independent case IDs, related-document group IDs, split, truth, label origin, source/domain/language/content type, result status, a complete probability distribution for completed cases, and optional latency/token usage. For a Choice task:

```sh
python scripts/evaluate_typed_decisions.py validation.json test.json \
  --task relevance --labels yes no --model-version jev-1.13.0 \
  --rubric-version relevance-v1 --release-criteria criteria.json \
  --coverage-requirements coverage.json \
  --output report.json
```

The coverage file declares `content_types` (normally news, blog, paper, transcript, book, note), `languages`, `length_buckets` (`short`, `medium`, `long`), `case_kinds` (`missing_evidence`, `quotation`, `negation`, `embedded_instruction`) and `min_label_support`. Each case then carries its length/case kind, pinned `model_version` and `rubric_version`, and exact `source_binding` including a content hash. Validation and test coverage gaps are reported and prevent a release gate from passing. The runner requires release criteria before opening the held-out test. Named `baseline_predictions` can compare `local_default` and `calibrated_full_window` on the same cases.

This evaluator makes no provider call. It checks split and group separation, selects an acceptance threshold on validation only, and reports held-out coverage, macro/per-label F1, Brier score, calibration error, latency, language/content-type strata, and estimated input cost. Predeclared release criteria are hashed into the validation policy and assessed only on test cases. If every held-out case supplies a baseline status/prediction, it also reports baseline macro F1 on the same cases. If every case supplies `total_cost_usd_micros`, it sums actual retry-inclusive cost separately from the token-price estimate. Unavailable and abstained cases stay in the denominator. Fixture labels can verify the pipeline but can never pass a release gate; human benchmark quality must be established independently. Passing gates still requires a separate human adoption decision. The typed-decision evaluator currently expects a categorical distribution. Separate binary Noul and multilabel frame task policies require task-specific analysis before adoption.

## Current task adapters

- Awareness semantic suggestions are in `src/kb/intake_decisions.py`. The suggestion contains item/session versions, objective relevance, urgency, optional novelty comparisons, source excerpts and action advice. Discard advice requires a task calibration ID and configured relevance ceiling. Acceptance calls the existing atomic triage command after rechecking versions; the MCP acceptance tool also verifies the durable run.
- Epistemic statement-kind classification is in `src/kb/epistemic_decisions.py` and is exposed as the source-grounded `suggest_jev_epistemic_kind` MCP tool. It checks caller text against an authorized exact source version before disclosure, preserves selected-label probability separately from vendor confidence, returns `truth_verified: false`, and keeps evidence assessment separate. `evaluate_jev_epistemic` compares held-out Jev labels with the local rules and reports attribution, hedging and quoted-allegation coverage. It does not mark the task ready without independent human labels.

Other uses are tracked individually in [the Jev milestone](https://github.com/Ikey168/Noesis/milestone/41). They should be enabled only after their task-specific acceptance criteria and held-out evaluations are met. TypeSafe documents current [question types](https://docs.typesafe.ai/primitives), [model limits](https://docs.typesafe.ai/models), and [failure modes](https://docs.typesafe.ai/model-jaggedness/jev-1.13).

`decision_task` and `suggest_jev_task` expose bounded question plans for stance, multilabel frames, both systematic-review screening stages, shortlist reranking, answer support, claim-pair relations, intake routing, review priority, methodology, entity/source matching, semantic revision significance, eligible-source selection, factual-claim detection, check-worthiness, sentiment, and attribution. Supply the task-specific `parameters` plus exact source versions; `src/kb/jev_tasks.py` validates them and builds related questions in one request. These are preview paths. Each owning subsystem must still apply its review, calibration, citation and acceptance rules before a suggestion affects the knowledge base.
