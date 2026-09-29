# Native intake mode extensions

These tools use the current caller's Noesis namespace and owner scopes. They do
not establish access to an external Modulo plugin record.

## Externalization: find a procedure

`search_intake_playbooks(namespace, task, environment?, limit?)` searches up to
500 owner-visible playbooks. It checks the current source-reference access for
every returned playbook, ranks task-word overlap, and returns exact playbook
revisions, draft/rehearsal state, and linked concept references. Open a match
with `inspect_intake_playbook`, then use the existing guided-run tools. A linked
concept is an input to Internalization; a guided procedure run does not prove
unaided recall. Search is lexical and may miss synonyms.

## Creation: build adapters for other artifact types

`post`, `documentation` and `teaching_material` are exported directly from the
reviewed authored report. `slide_deck`, `static_site`, `code_package` and
`dataset_release` projects follow the same attach → review → finish flow, and
are then built with `build_intake_creation_artifact(namespace, project_id,
expected_revision, idempotency_key)`.

- **Adapter contract.** A deployment-configured adapter for that type receives
  the verified creation export. It must return `artifact_locator`,
  `artifact_sha256` and `adapter_version`.
- **Idempotency.** The call is reserved once per idempotency key, and retries
  return the stored receipt.
- **Failures.** A malformed or failing adapter call is recorded as
  `indeterminate`.
- **Export.** `export_intake_creation` includes only completed build receipts
  for the current project revision.
- **No publishing.** Builds never authorize publication
  (`publication_authorized: false`).

The public server configures no build adapters and reports `unavailable`.
Other artifact types are rejected with `unsupported_artifact`.

## Externalization: procedure kinds and automated steps

`promote_intake_work_procedure` turns completed work into a versioned draft
procedure. The source can be a Problem-Solving session (verified), a Creation
session or a Deep Research session. Supported `artifact_kind` values:

| Kind | Extra field | Rule |
| --- | --- | --- |
| `playbook` | none | |
| `checklist` | none | Manual steps only; no automation. |
| `template` | `template_body` | |
| `default_configuration` | `settings` | 1–100 scalar defaults. |
| `automation_rule` | `trigger` | At least one automated step. |

Revisions keep these invariants. Search results include the kind.

Automated steps have the form `"automation": {"action", "parameters"}` and use
the Problem-Solving allowlist (`service.restart`, `ticket.create`) with the same
deployment-owned adapter registry.

1. `preview_playbook_step_automation` shows the exact step and whether its
   adapter is available.
2. `execute_playbook_step_automation` needs the preview hash, the expected run
   revision, an idempotency key and a correlation ID. It reserves durably
   before calling the adapter once, then records the step with its
   `execution_receipt` (`basis: adapter_execution`).
3. Retries return that receipt. An uncertain adapter outcome records a failed
   step that needs manual recovery.

An automated step cannot be marked passed by a caller report. Trust is shown by
`inspect_intake_playbook`:

- `draft`: saved, never rehearsed.
- `rehearsed_reported`: the latest completed, verified guided run was
  caller-reported.
- `executed_verified`: every automated step in that run has a completed
  adapter receipt. The date, environment and receipts are listed.

The public server configures no adapters, so execution reports `unavailable`
there.

## Internalization: source-pinned practice

`draft_intake_practice_pack` accepts 1–20 selections from current
`exploration_source`/`intake_feed_item` references or a current Research Bundle
concept/Evidence Card. Bundle selections identify the exact item with a
`locator.section` such as `concepts/concept-1` or `evidence_cards/card-1`.
Each selection also names a practice kind: `recall`, `procedure`, or
`explanation`. It generates prompt cues and exact answer references. The draft
has no answer and requires author review. Bundle and cited-source revisions
are rechecked when creating or reviewing a pack; corrected evidence pauses new
practice until the card is revised.
`create_reviewed_intake_practice_pack` rechecks the source version and draft
hash, then requires an approved prompt, author-supplied answer, and mastery
criterion for every card. The resulting pack uses the existing durable review
history and schedule. Revising one card resets that card's current schedule;
unchanged cards retain their progress and historical reviews stay available.
Self-reported passes remain distinct from human-assessed mastery.

## Internalization: skills and independent assessment

A skill gives practice and procedure evidence a stable identity:
`create_intake_skill` creates `skill:<hash>` with mastery criteria plus links to
practice cards and playbooks, and `revise_intake_skill` changes them.
`inspect_intake_skill_evidence` lists dated evidence by basis:

- `self_reported_unaided_recall` or `self_reported_assisted_recall`, from
  assessed practice reviews;
- `caller_reported_rehearsal` or `adapter_executed_procedure`, from linked
  procedures (procedural execution, not unaided mastery);
- `independent_assessment`, recorded with `assess_intake_skill`.
  `assess_intake_skill` requires `knowledge:intake:review`, and the owner
  cannot assess their own skill.

`mastery_demonstrated` is true only when every mastery criterion has a passing
independent assessment of the current skill content. Revising the criteria or
links starts a new content revision, and earlier assessments stop counting.
Self-reports, counts and procedure runs never establish mastery.

## Iteration: route an observed gap

After `record_intake_iteration_outcome`,
`route_intake_iteration_gap(namespace, session_id, command_key,
expected_revision, target_mode, reason, intent, inputs?, references?)` creates
an upstream session and appends its identity and reason to the original
Iteration cycle in one transaction. The stable command key makes retries
idempotent. The child session has a versioned origin link to its parent. A
failed child creation leaves the parent untouched.

Decision Records also have a native measured cycle:
`start_intake_decision_iteration` pins an owned revision,
`propose_intake_decision_revision` stores a complete validated proposal with
before/after rationale, and `accept_intake_decision_revision` appends the
outcome and proposal digest to the new authoritative decision revision in the
same transaction as the Iteration receipt. A stale revision or lost
decision-write access prevents acceptance. Authored reports also have a
revision-pinned cycle: `start_intake_report_iteration`,
`propose_intake_report_revision`, and `accept_intake_report_revision` commit the
accepted snapshot and exact outcome receipt atomically to the original report
history. Completion requires that accepted report revision to remain current.
Research Bundle concepts use `start_intake_concept_iteration`,
`propose_intake_concept_revision`, and `accept_intake_concept_revision`; the
cycle pins a bundle revision and concept ID, preserves the concept identity, and
records the accepted change in bundle history. Modulo-owned notes use
`start_intake_modulo_note_iteration`, `propose_intake_modulo_note_revision`,
and `accept_intake_modulo_note_revision`. Those operations retain a pinned
source snapshot and create a Noesis-local candidate receipt. They do not recheck
live Modulo access or write back to the plugin; accepted results explicitly say
`modulo_access_state=not_checked_by_noesis` and `writeback_state=not_written`.

## Problem-Solving: propose and execute a bounded action

`propose_problem_action` accepts only `service.restart` with a service and
reason, or `ticket.create` with a project, title, and description. It records
the proposal and rationale without invoking an integration. Use
`preview_problem_action` to inspect the exact proposal revision and adapter
availability, then `consent_problem_action(..., consent=true)` to bind the
owner's consent to that proposal and revision. `execute_problem_action` then
requires the current owner, intake-write and namespace-write scopes, the exact
consented revision, an idempotency key, and a correlation ID.

The adapter registry is deployment-owned and empty in the public server. With
no configured adapter, execution returns `unavailable` and does not reserve or
claim an external action. An injected allowlisted adapter is called once after
a durable reservation; the session and receipt store the adapter result. A
retry with the same owner, session, revision, idempotency key, and correlation
ID returns that receipt. A reservation with an uncertain outcome is not called
again. This local seam does not prove that a service restarted, a ticket was
created, or a user was unblocked; those need a configured integration and a
separate observed verification.

## Maintenance: inspect failures and impact

`scan_intake_maintenance` includes failed, retrying, and partial source-pack
worker jobs for callers with operator or maintenance-admin scope. Each finding
names the job status, next retry, and last successful generation time, when one
exists. Use the existing maintenance job inspection and retry tools to act on
those findings; a checklist disposition is only a report.

`preview_intake_maintenance_impact(namespace, finding_id, action)` accepts
`refresh`, `archive`, or `delete` for a current visible finding. It lists
owner-visible sessions, practice packs, playbooks, and retention objects that
depend on the impact root. For failed worker jobs, the finding target remains
the job ID and `impact_root` identifies the processed source pack. With
retention-read scope it also reports local status, dependencies, pins,
generation, object class, and active holds. The preview never authorizes
deletion. It discloses its scan limits and cannot check remote Modulo
dependents, plugin access, or cross-system recoverable history; those actions
require their own version checks and authorization.

## Maintenance: execute a previewed action

`execute_intake_maintenance_action(namespace, session_id, command_key,
expected_revision, finding_id, action, preview_hash, confirm_delete?)` runs one
checklist action for a finding in the session snapshot. Pass the
`preview_hash` from `preview_intake_maintenance_impact`; if dependents, holds
or revisions have changed since then, the call fails with `impact_changed`.

| Finding | Action | Effect |
| --- | --- | --- |
| failed worker job | `refresh` | Requeues it through the existing job retry. Needs `knowledge:maintenance:admin`. |
| superseded research source | `repair` | Re-pins the project link to the current verified source revision. Evidence citing the old revision is flagged for review. |
| research project | `archive` | Appends an archived project revision. Earlier revisions stay readable. |
| any retention-registered target | `delete` | Requires `confirm_delete=true` and retention admin + execute scopes. Runs a retention GC plan, which is blocked by active holds, pins or retained dependents, and leaves a tombstone. |

The action and its `noesis-intake-maintenance-action-v1` receipt
(`basis: executed`) commit together in one transaction, and the command key
makes retries return the stored receipt without repeating the action. Other
target/action pairs return `action_unsupported`; record a reported disposition
for those instead. Remote Modulo dependents are still not checked.

## Acceptance still requiring live evidence

The native paths above have deterministic replay, restart, and access tests.
The issue-level Modulo → Noesis → same-plugin-record journeys, plugin-state
migration, real user mastery, report/concept/Modulo-owned Iteration history, scoped external
automation receipts, and live 30–60 minute Maintenance review remain open
until their actual integrations and outcomes can be exercised.
