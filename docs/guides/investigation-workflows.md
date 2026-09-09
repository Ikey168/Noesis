# Recurring investigation workflows

Investigation templates, cited-evidence alerts and completed-run comparisons extend the existing project, subscription and report contracts. They use local DuckDB persistence and the Knowledge Engine MCP server. No new model, hosted provider, scheduler or notification service is required.

## Templates — #1529

`create_investigation_template(namespace, request_key, definition)` registers a versioned template. Definitions contain a name, description, named text parameters, questions, success criteria, domain/namespace scope, pinned source-pack IDs/versions and report section titles. Use `${parameter}` placeholders in questions, success criteria and the report outline. Parameters are substituted once as text; their contents are not evaluated or expanded recursively.

Two examples are included:

- [Berlin regulatory review](../../config/investigation_templates/berlin-regulatory-review.json), using `official-political-records` 1.0.0. During acquisition planning, select relevant EU/German sources from this broader pack; the template does not imply every pack source covers Berlin.
- [Research literature review](../../config/investigation_templates/research-literature-review.json), using `research-discovery` 1.1.0.

Pass the parsed example as `definition`, then retain the returned `template_id` and revision:

```json
{
  "namespace": "research",
  "template_id": "<returned template_id>",
  "revision": 1,
  "parameters": {
    "topic": "Berlin solar funding",
    "period": "2026-08-01 through 2026-08-31"
  }
}
```

Use those arguments with `preview_investigation_template`. Preview resolves installed source-pack versions and shows proposed questions, report sections and credential availability without network access. Missing pack versions prevent instantiation; missing credentials are shown explicitly and do not prevent creating an unexecuted project. Pack enablement, source selection, licenses, network rules and operational quotas are checked later by the existing acquisition preflight. Preview is not permission to execute a source.

Call `instantiate_investigation_template` with the same arguments plus a fresh `request_key` and explicit `budget` (integer `tokens`, `requests`, `usd_micros`; omitted dimensions default to zero). The result is an ordinary research project with `template_origin`: exact template revision, parameters, resolved report outline, and source-pack manifest hashes. It contains no copied evidence or completed runs. The outline is retained for subsequent report drafting; no empty report with a fabricated snapshot is created.

Repeating an identical request is idempotent. Changing its parameters, template or budget under the same project request key fails. Project edits and subsequent template revisions are independent. `revise_investigation_template` and `archive_investigation_template` require `expected_revision`; a stale revision fails instead of overwriting concurrent work. Historical template revisions remain inspectable, and archival prevents new instantiations. Use `inspect_investigation_template` and `list_investigation_templates` for discovery and reopening.

Access uses `knowledge:projects:read` / `knowledge:projects:write`, current owner and namespace access, and declared domain permissions. Source-pack domains are rechecked during preview and instantiation. No credential values are copied into project/template provenance.

Limits: 64 KiB template definition, 30 parameters, 1,000 characters per parameter value, 50 entries per text section and 20 pinned packs. Listing returns at most 100 templates. Templates are instantiated in their namespace; scope remains explicit rather than being inherited from another project.

## Cited-evidence alerts — #1530

Subscribe to an explicit project or report revision:

```json
{
  "target": {
    "kind": "report",
    "namespace": "research",
    "id": "<report_id>",
    "revision": 1
  },
  "request_key": "monthly-report-evidence-watch",
  "categories": ["revised", "withdrawn", "notice", "unavailable", "recovered"],
  "batch_size": 50
}
```

Call `subscribe_cited_evidence`, retain its `subscription_id`, then call `evaluate_cited_evidence_subscription` when new committed evidence should be checked. Monitoring is an explicit local evaluation; this feature does not start provider polling or install a background schedule. The subscription covers the pinned target revision. To cover newly added citations in a revised report/project, create a subscription for that revision and unsubscribe the earlier one when appropriate.

`poll_cited_evidence_alerts(subscription_id, cursor)` returns ordered, bounded batches with a durable cursor and `has_more`. Persist the returned cursor even when a batch contains no visible alerts: baseline/current observations and category-filtered events can advance it. Burst changes retain individual observation/source links in batches rather than being discarded by a summary. Intermediate source revisions between evaluations remain available in source history; polling does not claim to have assessed unseen intermediate states.

Each alert includes the subscribed target, affected report section/assertion or linked project finding, exact dependency and before/after revision metadata, an explicit category/reason and actions for inspecting document revisions and entering the existing report-assessment or project workflow. The original source body is not copied into the alert. Initial unchanged citations are silent; already changed citations can produce an alert on the first evaluation.

The observation cursor is distinct from native ingestion generations. Source-pack watermarks are local to each pack, and direct committed observations can use zero. The adapter commits a content-addressed citation observation into the existing subscription watermark/event machinery, with `origin=citation-observation-v1`. Repeated identical observations replay without duplicate events. A recovery to earlier content is a new transition. Pending/unavailable inputs are explicitly reported and never represented as a confirmed source deletion. Withdrawal inferred by source-change heuristics is a review notice; confirmed-withdrawal alerts require explicit source lifecycle metadata.

`acknowledge_cited_evidence_alert` records acknowledgment only. It does not approve evidence, change an assertion, accept a report edit or merge entities. `set_cited_evidence_subscription_status` accepts `paused`, `active` and `deleted`; the last unsubscribes while retaining history. `inspect_cited_evidence_subscription` returns settings and checks target access. Report alerts can open `assess_authored_report_changes`, followed by the existing propose/review/accept-or-reject flow.

Access requires `knowledge:subscriptions:read` / `knowledge:subscriptions:write`, explicit namespace read scope, and current target/project/report and cited-document permissions. Polling and the generic subscription delivery path recheck retained target access. Revoked access blocks historical payload delivery. No email or Slack messages are sent. A subscription supports at most 1,000 dependency/affected-target bindings, an 8 MiB observation, and batches of 1–100 raw events.

## Completed investigation comparisons — #1531

Use `create_investigation_comparison(namespace, request_key, left, right)`, selecting two retained, completed recipe runs already linked in the corresponding project revisions:

```json
{
  "namespace": "research",
  "request_key": "august-versus-september",
  "left": {
    "project_id": "<project_id>",
    "project_revision": 4,
    "run_id": "<first_completed_recipe_run_id>",
    "generations": {"research": 7}
  },
  "right": {
    "project_id": "<project_id>",
    "project_revision": 8,
    "run_id": "<second_completed_recipe_run_id>",
    "generations": {"research": 11}
  }
}
```

Select project revisions representing each run's research context. The adapter validates project/run linkage, completed status, owner, exact finding revisions and namespace generation bounds. It does not run research or silently relink current findings. Runs from separate projects are allowed, with incompatible questions or scope disclosed rather than assigned a winner.

The immutable comparison contains source-reference changes; existing finding change classifications (new evidence, changed method/configuration, changed interpretation or mixed changes); native contradiction-relation state transitions; and related research-gap transitions. Contradictions are explicitly linked relation findings whose native predicate is `contradicts`, `contradiction` or `refutes`; this comparison adds no semantic contradiction detector. Gap revisions must belong to selected committed generations and predate the corresponding run's completion. Later reviews cannot rewrite historical questions, even when their gap revision retains an earlier generation number.

Coverage describes linked retained evidence and recorded related gaps, not all possible knowledge. Missing generations, unavailable references and unequal coverage remain explicit; unavailable sides do not masquerade as substantive finding removals or additions. Cost differences use cumulative recorded spending at the selected project revisions, not independently measured per-run provider invoices. Recipe/input/receipt fingerprints and the comparison producer version are retained.

`inspect_investigation_comparison` reopens the stored artifact and reports current availability separately, without replacing original differences with newer evidence. It rechecks current ownership, namespace, recipe and document access. `export_investigation_comparison` uses the existing report Markdown renderer and attaches the structured comparison, hashes and limitations. This is a derived comparison export, not a newly authored evidence report or an automatic merge of conclusions.

Creation uses `knowledge:projects:write`; inspection/export use `knowledge:projects:read`. Both sides additionally require current `knowledge:recipes:read`, relevant namespace/document access and `knowledge:gaps:read` when gap history is present. The current owner must be authorized for both project revisions and runs. No export expands these rights.

Limits: 100 selected namespace generations, existing project link limits, at most 1,000 related gaps per namespace and an 8 MiB artifact. Retry the same request key to reopen an identical comparison; reuse with different selectors is rejected.

## Persistence and verification

Creation operations initialize the additive `investigation_templates`, `investigation_template_revisions`, `citation_alert_acknowledgments` and `investigation_comparisons` tables. Existing project revisions, report revisions, source revisions, gap history, recipe runs and subscription events remain authoritative. Read operations do not create tables. The MCP capability catalog includes all sixteen new tools and classifies writes/scopes explicitly.

Focused verification:

```sh
.venv/bin/python -m pytest tests/unit/kb/test_investigation_templates.py tests/unit/kb/test_citation_alerts.py tests/unit/kb/test_investigation_comparisons.py tests/unit/tools/test_investigation_workflows_mcp.py -q
.venv/bin/python scripts/generate_mcp_catalog.py --check
```

The tests use authored contract fixtures and local recipe adapters. They verify persistence, isolation, access revocation, history and public invocation; they do not replace the independent human/provider evaluations in [the follow-up plan](../development/further-evaluations-and-access.md).
