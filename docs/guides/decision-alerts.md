# Decision review conditions

`create_decision_condition_watch` pins explicit conditions to the current decision revision and records their initial state. Conditions support exact evidence revision changes, evidence changes associated with a declared assumption, and quantitative thresholds with explicit namespace, metric, provider, series, period, unit, comparison, and decimal threshold. Watching an assumption does not turn a source change into proof that the assumption is false.

`poll_decision_condition_watch` checks committed evidence and creates a durable review task when a condition has new triggering evidence. Registration establishes the baseline; it does not alert on an already-present condition. Repeated polls suppress duplicates. Unknown, missing, preliminary, unsourced, or conflicting quantitative observations yield uncertain assessments. The pinned project constrains condition namespaces; current decision, project, source, and metric access is rechecked.

Every task retains exact before/after evidence and the pinned decision revision. Delivery uses the existing change-brief policies, subscriptions, delivery receipts, and acknowledgement machinery. A subscription targets the exact generated brief, avoiding unrelated brief delivery or repeated earlier alerts. Briefs contain task/evidence references and hashes; detailed decision evidence stays in the owner-scoped review task. Failed delivery leaves a durable pending task for the next poll to retry. Delivery windows reject more than 500 candidates instead of silently truncating them.

Use `list_decision_review_tasks` to inspect tasks and `acknowledge_decision_review_task` to acknowledge one. Acknowledgement does not revise the decision. After an explicit `revise_research_decision`, the acknowledgement can independently link that later revision. A changed decision requires a new watch to explicitly bind updated conditions. Polling is caller/scheduler driven; no external destination or autonomous scheduler is enabled.

Scopes include decision read/write, project read, and current evidence permissions. Polling with delivery additionally requires existing change-brief read/write/deliver scopes; acknowledgement of delivered tasks requires brief-deliver scope. A successful threshold match or delivered alert does not verify a substantive claim or select a new action.

Tests cover source-change deduplication, interrupted delivery, exact-brief filtering, acknowledgement versus revision, evidence access revocation, metric thresholds, and uncertain observations through store and MCP APIs.
