# Persistent research projects

Projects organize existing plans, runs, hypotheses, evidence, findings, and snapshot references. Their question history and committed expenditure survive process restarts. Creating or reopening a project does not execute research.

The `noesis-research-project-v1` contract is exposed through `create_research_project`, `inspect_research_project`, `list_research_projects`, `revise_research_project`, `archive_research_project`, and `record_research_project_expenditure` on the knowledge-engine MCP server.

## Access

Every call uses the current principal and scopes. The owner needs `knowledge:projects:read` or `knowledge:projects:write`, namespace read/write scopes for every namespace in the project, and `domain:<name>:read` for each selected domain. Writes require namespace write access. `operator` can administer projects. Revoking a scope also prevents access to historical revisions; saved project state does not preserve old authorization.

## Example

Create an investigation with two domains:

```json
{
  "namespace": "research",
  "request_key": "rate-policy-2026",
  "questions": ["How did rate policy affect investment?"],
  "success_criteria": ["Compare economics and policy evidence, with citations and limitations"],
  "scope": {"domains": ["economics", "policy"], "namespaces": ["papers"]},
  "budget": {"tokens": 100000, "requests": 100, "usd_micros": 10000000}
}
```

Reuse `request_key` with the same request to recover its identity. Reusing it with different inputs fails. Save the returned `project_id` and `revision`. Each revision or expenditure call takes `expected_revision`; a stale or concurrent update returns `revision_conflict`.

Append a source reference through `revise_research_project`:

```json
{
  "namespace": "research",
  "project_id": "<returned project_id>",
  "expected_revision": 1,
  "add_links": [{
    "kind": "evidence", "id": "claim-17", "namespace": "papers", "revision": 3,
    "locator": {"document_id": "paper-2", "revision_id": "revision-3", "start": 120, "end": 208}
  }]
}
```

Changing questions appends a revision and retains earlier outputs with their original question revision. Reopen any revision with `inspect_research_project`. Record each committed run's integer costs once using its stable receipt ID; replay cannot double charge. Budget fields omitted at creation are zero. This accounting operation does not reserve future execution costs; bounded research execution is a separate workflow task.

## Retention

Projects use `references-only` retention: archiving preserves the project ledger, but does not pin external evidence indefinitely or renew a research-session token. Snapshot references store IDs and generations, never bearer-token fields. Inspection reports expired, unavailable, or inaccessible sessions. Other references are marked `not_checked`, and `generation_verified: false` explicitly means historical generation availability has not been verified. Resolve those references through their authoritative stores before using them as evidence or an executable baseline.

Archived projects remain inspectable and immutable. Existing plans, hypotheses, and runs retain their own lifecycles and authorization.
