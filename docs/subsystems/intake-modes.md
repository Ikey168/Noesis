# Information intake mode sessions

The `noesis-knowledge-engine` MCP server exposes a durable session ledger for the ten information workflow modes in [the intake roadmap](https://github.com/Ikey168/Noesis/issues/1569). It is a coordination layer over existing Noesis stores, not a second repository of sources, evidence, decisions, or reports. Modulo can retain its own planning and user-edited note state and store stable Noesis session and artifact references as specified in [Modulo #474](https://github.com/Ikey168/Modulo/issues/474).

The session response follows [`noesis-intake-session-v1`](../../contracts/schemas/jsonschema/noesis-intake-session-v1.json). The schema covers current and historical revisions, including redacted references after access revocation.

[The acceptance matrix](intake-acceptance-matrix.md) separates current native operations from Modulo handoffs, deterministic checks, live evidence, and user-assessed outcomes for every mode.

## Supported contract

Use `discover_intake_modes`, `route_intake_mode`, `start_intake_mode`, `inspect_intake_mode`, `list_intake_modes`, `command_intake_mode`, `export_intake_mode`, `verify_intake_mode_export`, and `export_modulo_intake_handoff` on the existing Knowledge Engine MCP server. Routing applies the ten intent questions in priority order and accepts an explicit user override. A session has an owner and namespace, a mode-specific time budget, a revision, optional origin session, versioned references, a command history, and a status. `command_intake_mode` supports `record`, `pause`, `resume`, `complete`, and `cancel`. It requires a fresh `expected_revision` and a stable `command_key`; replay of the same command returns the original revision, while reuse with a different action or payload fails. `start_intake_mode` likewise requires a stable `request_key`.

`discover_intake_modes` is a public catalog, not a readiness check. `preflight_intake_mode(namespace, mode?)` is a read-only, caller-scoped check: it verifies current namespace access, reports missing mutation/domain scopes, counts only the caller's enabled feed subscriptions, distinguishes native entry points from a complete journey, and lists mode-specific blockers. Its `source_mode` remains `unknown_live_or_fixture`, and live reachability, model, execution, and unattended delivery results stay unverified unless an operation actually checks them. `transport_accessible` means this scoped MCP call succeeded; `native_start_possible` means known scopes and local prerequisites permit the listed native entry points. Neither means the user's outcome or a live deployment is accepted.

MCP clients can discover the `noesis://intake/{namespace}/{session_id}` resource template for a current session and `noesis://intake/{namespace}/{session_id}/revisions/{revision}` for an exact historical revision. Reading either rechecks the same current intake scope, namespace, and owner access as `inspect_intake_mode`; a denied read is an MCP error. The `start-information-intake` prompt accepts a mode, namespace, and intent, then guides clients to discover requirements and use a stable request key. Prompt discovery conveys no private session data and does not establish service or data readiness. HTTP resource/prompt discovery and a denied cross-owner read are covered by the transport test.

The same MCP server also publishes current and exact-revision resource templates for `noesis://intake/playbooks/{namespace}/{playbook_id}` and `noesis://intake/practice/{namespace}/{pack_id}`, plus current guided runs at `noesis://intake/playbook-runs/{namespace}/{run_id}` and current/historical reviews at `noesis://intake/practice-reviews/{namespace}/{review_id}`. Append `/revisions/{revision}` to a playbook, pack, or review URI for an exact version. Each read calls the authoritative store under the current caller's intake, namespace, owner, and referenced-namespace access. A practice-pack resource contains author answers for editing; review resources keep the answer hidden until that review revision records a reveal. These resources are for clients already authorized to read the objects, not public publication links.

Readers need `knowledge:intake:read` and `namespace:<name>:read`; writers need `knowledge:intake:write` and `namespace:<name>:write`. The owner is checked on each non-operator call. For HTTP, configure `NOESIS_MCP_AUTH_TOKENS_FILE` as a private JSON object mapping bearer tokens to `{ "client_id": "user-id", "scopes": [...] }`; each token must be at least 32 characters and each client ID unique. Intake tools take the authenticated client ID and scopes from the request. Unauthenticated HTTP intake calls and the legacy shared token with no scopes cannot mutate sessions. Stdio retains the configured local principal and scopes. A reference to another namespace requires current read access to that namespace. Revoked references and derived session data are redacted on inspection, and completion/export fails while linked access is missing. An export includes every session revision and a SHA-256 digest for portable integrity checks; the digest alone does not authenticate the exporter.

The request identity is read through FastMCP's [access-token context](https://gofastmcp.com/v2/servers/context#access-tokens). The token file is a local integration option, not a substitute for a managed identity provider in a shared deployment.

For a transition, call `start_intake_mode` with an `origin` containing the previous `session_id` and a reason. The new session retains the prior mode and revision in its origin record. An optional `workspace_links` list carries versioned, opaque Modulo workspace/item/project/note/task IDs for return navigation. Do not copy source content into the session: attach a `{kind, id, namespace, version, locator?}` reference to the authoritative object. The session ledger does not validate that an arbitrary referenced object exists or a Modulo link is currently accessible, so callers must resolve IDs through the authoritative subsystem before relying on them.

## Deep Research topic and project handoff (in progress)

`start_intake_research_topic` creates an owner-scoped Deep Research session and authoritative research project in one transaction. The caller supplies questions, a Definition of Done, explicit research scope and budget, optional earlier-session origin, versioned source references, and Modulo workspace links. Saved `exploration_source` and `intake_feed_item` references must resolve to an exact historical revision owned by the caller; those sources are pinned in the project's initial links. `inspect_research_project` reports each pinned intake source as current, superseded, or unavailable, without copying its content into the project. Other session references remain caller-supplied pointers. The session pins the project's first revision and carries its stable ID. A repeated request key replays both identities, including after the project has been revised; a missing source, capacity, or authorization failure leaves neither record. The existing intake limit defaults to three active or paused Deep Research sessions per owner and is configurable with `INTAKE_ACTIVE_RESEARCH_LIMIT`. The tool requires both intake and project write scopes.

`save_intake_research_bundle` now records a versioned synthesis for that project. Its Evidence Cards contain exact character offsets and quotes checked against pinned feed or Exploration source revisions. Claims identify supporting and contradicting cards; a high-confidence claim requires supporting cards from two different source hosts. Concepts, a brief, mental model, map, known/uncertain/unresolved points, and each Definition of Done review cite card IDs. These are authored interpretations of verified source spans, not automatically verified conclusions. `inspect_intake_research_bundle` rechecks current source revisions and project access. A corrected source or revised project makes the bundle unready until reviewed and saved again. The revision chain can be exported and its digest verified with `export_intake_research_bundle` and `verify_intake_research_bundle_export`.

For paired topics, `command_intake_mode(complete)` now requires one current, ready `research_bundle` reference whose project matches the session. Caller-supplied artifact names and `definition_of_done_met` cannot complete a paired topic. An uncited or incomplete synthesis remains a draft. `inspect_intake_research_progress` and its durable assessment compose acquisition/extraction/query receipts, topic-level coverage, bundle readiness, blockers, and reviewed Definition of Done under [#1572](https://github.com/Ikey168/Noesis/issues/1572). A caller-supplied source-origin grouping is only an assertion and cannot make a high-confidence claim ready. `review_research_claim_independence` requires `knowledge:intake:review` and records the reviewer, method, rationale, claim fingerprint, and exact source pins. Only one review is accepted per claim and bundle revision; revise the bundle to reopen review. Its `verified` flag means the authorized review record matches that bundle revision; it does not mean Noesis independently established source lineage. Different hosts alone do not prove separate authorship or corroboration.

`inspect_intake_research_progress` composes the paired session and project, pinned source status, bundle readiness reasons, reviewed Definition of Done criteria, and up to 20 existing project loops in one current-access view. For each loop it shows bounded action status, stop reason, source coverage counts, and acquisition/derive/query checkpoint status and hashes. It resolves a recipe run by the loop's stable run key even when the enclosing loop action has not committed its final result. Detailed checkpoint receipts require `knowledge:recipes:read`; without that scope the view reports a limitation. Its automatic structural assessment names missing stages, inadequate configured coverage, stale source pins, unready synthesis fields, and unmet review criteria with a suggested next action. It never treats completed stages, source counts, source-origin assertions, or author-reviewed citations as proof of substantive correctness. An optional `coverage_assessment_id` adds caller-selected status counts; it is not bound to the project.
`assess_intake_research_progress` stores that assessment and its exact progress snapshot under a stable command key. Retrying the key after a restart returns the saved snapshot; a new key assesses current state. `inspect_intake_research_assessment` returns a saved receipt while current access to the topic and project remains valid. A newer bundle or project change requires a new assessment. These receipts connect the existing resumable loop and recipe checkpoints to bundle readiness without introducing a second acquisition or evidence store. Modulo's plugin-state journey and representative live acceptance remain open under [#1572](https://github.com/Ikey168/Noesis/issues/1572).

## Native Awareness inbox (in progress)

The Knowledge Engine MCP server now exposes `subscribe_intake_feed`, `subscribe_intake_newsletter_input`, `ingest_intake_newsletter_message`, `list_intake_feed_subscriptions`, `refresh_intake_feed_inbox`, `list_intake_feed_inbox`, `inspect_intake_feed_item`, `preview_intake_feed_signals`, `save_intake_feed_signal_rule`, `list_intake_feed_signal_rules`, `preview_intake_feed_signal_rule`, `mark_intake_feed_read`, `decide_intake_feed_item`, `annotate_intake_feed_item`, `start_awareness_from_inbox`, `triage_awareness_item`, `triage_awareness_batch`, and `promote_awareness_item`. A feed subscription is owned by one caller and namespace; its URL must be public, credential-free HTTPS. `refresh_intake_feed_inbox` additionally requires `knowledge:intake:fetch` and fetches at most 50 configured feeds with up to 100 entries each. It uses the existing RSS/Atom parser and a bounded network transport without implicit redirects or proxy credentials. A `newsletter_feed` is a newsletter that explicitly offers RSS/Atom. For an explicitly configured sender, a caller can submit a normalized newsletter message with its original Message-ID and publication time; the response labels sender authentication `caller_supplied_unverified`. Mailbox polling and authenticated delivery still require an external adapter.

The inbox preserves a source item's stable ID, original URL, publication time, content revision history, and annotations independently from read and triage state. Saved signal rules are owner-scoped and versioned; previews explain case-insensitive keyword matches without changing read or decision state. Refreshing the same item does not reset a decision. Listing or inspecting an item does not mark it read. `start_awareness_from_inbox` snapshots at most 1000 unprocessed items into a 1–15 minute session. `triage_awareness_item` and `triage_awareness_batch` commit decisions to both the inbox and session in one transaction; a command replay returns the original revision. `promote_awareness_item` requires an explicit escalate decision and carries exact source and annotation references into Exploration, Deep Research, or Problem-Solving. A Modulo caller can supply a versioned `workspace_links` entry for its already-persisted intake item, so the new session handoff points back to that item. Other Awareness work still uses the recorded-only session contract.

## Native Exploration capture (in progress)

An Exploration session can start with `start_intake_mode` and no research objective or output. `capture_exploration_page` records a visited or saved HTTPS URL, a user note, and optionally caller-supplied readable text. Set `fetch_readable=true` with `knowledge:intake:fetch` to acquire a public HTML or plain-text page through a bounded, no-proxy, no-redirect transport; extracted text records its acquisition method. Caller-supplied text remains labeled as such. Source snapshots keep stable IDs and historical versions, available through `inspect_exploration_source`. `annotate_exploration_source` attaches an immutable, idempotent note and optional locator to the exact current source version; historical inspection shows annotations for that version. A command replay does not refetch the page. `visit_exploration_feed_item` adds an Awareness source to the trail without recapturing it or changing its source identity. `suggest_exploration_sources` scans up to 500 captured pages and 500 feed items owned by the caller; a feed visit can anchor a suggestion using its exact historical version. Suggestions return shared terms and two versioned source references. `lexical_overlap_v1` is a bounded keyword signal, not semantic or human-assessed serendipity. `decide_exploration_suggestion` durably dismisses or follows one suggestion; following adds a visit to the original source ID. Callers can pass `expected_candidate_version` to reject a suggestion whose candidate changed after display. A session can pause and resume through `command_intake_mode`; its completion still follows the recorded time box or an explicit escalation reason. Live quality acceptance remains to be implemented.

## Decision Support completion

Decision Support completion now resolves its versioned `decision` reference against the current, owner-accessible Decision Record. Exactly one current record must match the session's selected option and rationale. A nonexistent, superseded, or mismatched reference cannot close the session; earlier recorded revisions remain inspectable. `record_decision_evidence` enforces a declared item cap, relevance criteria, and stop condition while recording one assessed evidence reference in each revision under an idempotent command key. `inspect_decision_comparative_matrix` projects one pinned sensitivity receipt with the exact choice revision, criterion utilities, missing inputs, relative declared-utility advantages and disadvantages, assumptions, evidence references, scenario ordering, rationale, and review triggers. The evidence relevance and weighted utilities are author supplied; neither is independent proof or recommendation. Modulo decision review and task/project return links remain open under [#1573](https://github.com/Ikey168/Noesis/issues/1573).

## Problem-Solving trail

`start_problem_session` opens a typed Problem-Solving session with the symptom, environment/version context, urgency, and an observable success check. It needs no research project or bundle. A Modulo task or intake item can be passed as a versioned `workspace_links` entry; a dedicated plugin record can be recorded separately in `plugin_links`, with its plugin/account/record identity and representation authority. An interrupted prior session can be passed as `origin`. `record_problem_step` appends a timestamped `hypothesis`, `proposal`, `reported_attempt`, or `verification` with a summary, optional next action, and versioned Noesis source references. Attempts and verifications require an observed result; verification also requires an explicit passed/failed boolean. A proposal is never treated as an attempt. A failed check keeps the session open. Typed sessions accept `record_problem_step` for trail writes and reject generic `command_intake_mode(record)` updates, so a direct `verified=true` patch cannot bypass the trail. Completion requires the latest step to be a passed observed verification, alongside the existing verification summary. Each step and session command uses the existing owner/namespace checks, optimistic revision, stable key replay, and durable history.

`reported_attempt` records what the caller says happened; Noesis does not execute a fix or independently validate the success check through this tool. A configured allowlisted action's durable receipt retains the session's plugin links and correlation key. A source reference is versioned and access checked, but the intake ledger does not resolve arbitrary referenced object IDs. Authorized integration execution, Modulo Blueprint receipts, and live outcome acceptance remain work under [#1574](https://github.com/Ikey168/Noesis/issues/1574).

## Draft playbooks and guided rehearsals

`promote_problem_playbook` accepts only a completed typed Problem-Solving session whose latest verification was recorded as passed. It snapshots that session's versioned references, dedicated plugin links, and origin revision into a Noesis playbook with prerequisites, environment, ordered action/expected-result/recovery steps, a verification check, and source rationale. It creates a **draft**. Repeating the stable request key returns the same playbook; `revise_intake_playbook` stores an exact historical revision and resets its trust state to draft. Current owner, namespace, and linked Noesis source access is checked for reads and writes.

`start_guided_playbook_run` pins a playbook revision and environment. `command_guided_playbook_run` records each step's observed pass/fail result in order, permits a failed step to be retried after recovery, and requires a final observed verification before completing the run. Pause/resume and command replay are durable; `inspect_guided_playbook_run` resumes after restart. `inspect_intake_playbook` reports how many completed guided runs the caller has recorded against that exact revision. Caller-reported rehearsals yield `rehearsed_reported`. Steps can declare an allowlisted automation that runs once through a deployment-configured adapter with a preview, idempotency key and receipt; a verified run whose automated steps all have completed receipts yields `executed_verified`. Procedures can also be checklists, templates, default configurations or automation rules (see the [native mode extensions guide](../guides/intake-native-mode-extensions.md)). Modulo Blueprint round trips and real-world acceptance remain open under [#1576](https://github.com/Ikey168/Noesis/issues/1576).

## Native retrieval practice

`create_practice_pack` creates a versioned author-reviewed pack of 1–100 recall, procedure, or explanation cards. Each card has a prompt, an author-supplied answer, a textual mastery criterion, and 1–20 versioned source or concept references. An optional `plugin_links` list retains exact external plugin record identity separately from those Noesis source references; linked Noesis references are rechecked against current access. Answers are labeled `author_supplied_unverified`; creating a pack does not validate them. `revise_practice_pack` uses an optimistic revision and stable edit key, retains historical answers and attempts, and resets the current card schedule so changed material becomes due again. A pack can choose 1–20 strictly increasing review intervals in days (default: 1, 3, 7, 14, 30, 60, 120).

`list_due_practice` returns current due or overdue prompts without answers. For pinned feed or Exploration sources it also reports `source_status` and `reviewable`. A corrected or missing source keeps the prompt visible but prevents new reviews and further steps in an existing review until the card is revised against the current source; replaying a prior command still returns its historical receipt. `start_practice_review` pins the pack revision; `command_practice_review` requires a recorded caller-reported answer before answer reveal, then a self-assessment. Assisted attempts are labeled and cannot advance the unassisted schedule. A passed unassisted self-assessment advances to the next configured interval; a failed or assisted result returns the card to the first interval. Review commands use expected revisions and stable keys; `inspect_practice_review` returns historical state after restart and withholds the answer until a reveal was recorded. The practice history is distinct from procedural execution and no self-rating is reported as independently demonstrated mastery.

`export_practice_pack` provides the complete pack revision chain, review revision chains, current schedule, and a SHA-256 integrity digest. It retains plugin identity in each exact pack revision; this does not write back to that plugin. `verify_practice_export` checks that digest and revision order offline; it does not authenticate the exporter or independently validate the answers. Current owner, namespace, and referenced-namespace access is checked before export.

This is a native Noesis practice core, not the complete Internalization mode. Reviewable draft generation now accepts current feed/Exploration sources and selected concepts or Evidence Cards from a current Research Bundle; prompts still require author-supplied answers and criteria. Correction checks cover feed items, Exploration sources, Research Bundle items and ingested documents; opaque reference kinds report `not_checked`. Skills record independent reviewer assessments separately from self-reports. Modulo practice-plugin migration and UI, recorded human-assessed mastery, and live acceptance remain open under [#1577](https://github.com/Ikey168/Noesis/issues/1577).

## Creation projects over authored reports

`start_intake_creation` records an owner-scoped project with purpose, audience, a supported artifact type (`post`, `documentation`, or `teaching_material`), declared acceptance criteria, versioned research/decision inputs, optional Modulo workspace links, and separate exact `plugin_links`. Unsupported artifact types return an explicit error; this adapter does not build software or publish material. `command_intake_creation` attaches an existing Noesis authored report by its current ID/revision, records a pass/fail review against every criterion with author notes, finishes only after all checks pass, and can reopen the project. Every command needs an expected project revision and a stable key; a replay returns the same historical result. A newer report revision makes the attached snapshot stale and blocks finishing or exporting until it is reattached and reviewed. `inspect_intake_creation` exposes current or historical project state.

The authored report remains the authoritative draft, assertion, citation, and revision store. `export_intake_creation` returns its Markdown/report export alongside the project's revision chain and an integrity digest; it is available only after an author-reported accepted review. The export explicitly says `publication_authorized: false`. `handoff_intake_creation` opens a replay-safe Externalization, Internalization, or Maintenance session from a finished project and its current authored report revision, carrying both exact references and the originating Modulo workspace links. A changed report blocks a new handoff until the project is reviewed again. This handoff starts work; it does not generate a playbook or practice pack. A reported pass is not independent proof that an artifact meets its purpose or that its evidence is correct. Slide decks, static sites, code packages and dataset releases are built from the finished report through deployment-configured build adapters with idempotent receipts; none are configured publicly. Modulo creation editing, a verified external review, and live user acceptance remain open under [#1575](https://github.com/Ikey168/Noesis/issues/1575).

## Maintenance review over intake artifacts

`scan_intake_maintenance` returns owner-scoped findings for overdue practice cards, cards tied to corrected or missing intake sources, draft playbooks older than 30 days, failed caller-reported guided rehearsals, changed accepted Creation reports, corrected or unavailable sources pinned in accessible Research Projects, and source citations in accessible authored reports whose committed source revision changed or was withdrawn. Citation findings identify the report, assertion, source ID, pinned revision, current revision, and comparison reason; they never copy cited source content. The scan needs `knowledge:reports:read` for report review, skips citation comparisons when current evidence access is missing, checks at most 500 reports, and discloses scan limits. It groups exact duplicate content among the caller's accessible authored reports for review. Reports unchanged for at least 30 days become unlinked-content review candidates only when every available local reference scan is complete; it checks Creation projects, intake-session references, report monitoring/proposals, owner-scoped knowledge subscriptions, and persisted artifact dependencies. The finding records which local stores were checked, whether a retention object and active holds are visible, and that Modulo/external links were not checked. Incomplete report or reference coverage suppresses unlinked candidates. These are review candidates, not findings that content is safe to delete. Topic scanning requires `knowledge:projects:read` and the project's namespace/domain scopes; the scan checks at most 500 projects and discloses when that limit applies. Maintenance worker failures appear for callers with `knowledge:maintenance:admin`; findings include status, attempt count, latest completed generation time, and retry time where applicable. The queue caps at 100 findings and reports enabled coverage and limits. A failed guided rehearsal is a caller report, not proof of automated execution failure.

`start_intake_maintenance` snapshots that queue into a 30–60 minute Maintenance session. `record_maintenance_finding` records each review or deferral, with a stable command key and expected revision. `assess_maintenance_health` records the caller's criteria, observation, and acceptable result. A typed session cannot complete until every snapshot finding is reviewed or deferred and health is marked acceptable. These are caller-reported dispositions, not proof that a repair, refresh, archive, or deletion ran. The read-only impact preview resolves worker findings to their source-pack root and reports accessible Noesis dependents and local retention status, dependencies, pins, and holds. `inspect_maintenance_job` and admin-scoped `retry_maintenance_job` expose worker inspection and retry over MCP; retry requeues a job without claiming it ran. `execute_intake_maintenance_action` runs a previewed refresh (failed job), repair (re-pin a superseded research source), archive (research project) or retention-checked delete, bound to the reviewed preview hash and committed atomically with its receipt. Modulo dependents, the Modulo workbench, and live monthly acceptance remain open under [#1579](https://github.com/Ikey168/Noesis/issues/1579).

## Measured playbook Iteration

`start_intake_iteration` pins an owned Noesis playbook revision, expected outcome, stability criteria, and optional earlier session origin. `record_intake_iteration_outcome` captures caller-reported observed values, measurements, uncertainty, possible external causes, learning, and versioned evidence references. The references are caller-supplied links; the cycle does not infer causality or independently verify real-world observations. `propose_intake_playbook_revision` stores a complete reviewable draft with before/after rationale. `accept_intake_playbook_revision` uses the playbook's expected revision and stable edit key to write the accepted change and iteration receipt into the playbook's own revision history. A new playbook revision and session acceptance receipt commit in one transaction; a failed session write rolls back that new revision. The receipt includes the measured outcome and proposal digest. `review_intake_iteration_stability` records whether the user's criteria appear stable; a false result can still close a cycle, and a new cycle can link to the previous one. Typed Iteration completion requires the accepted revision and stability review.

Decision Records also support the native cycle: `start_intake_decision_iteration` pins an owned decision revision, `propose_intake_decision_revision` records a complete validated decision proposal, and `accept_intake_decision_revision` appends the measured outcome, before/after rationale, and proposal digest to the accepted decision revision history in the same transaction as the Iteration receipt. The original revision remains inspectable, and a stale decision or revoked decision-write scope prevents acceptance. This records the caller's observations; it does not assert that the changed decision caused them.

Authored reports and Research Bundle concepts also have version-pinned cycles:
`start_intake_report_iteration` / `propose_intake_report_revision` /
`accept_intake_report_revision` and `start_intake_concept_iteration` /
`propose_intake_concept_revision` / `accept_intake_concept_revision`. Accepted
changes are written into the original report or bundle history with a receipt
that retains the measured outcome and proposal digest. A Modulo-owned note can
start a local candidate cycle with `start_intake_modulo_note_iteration`, then
use `propose_intake_modulo_note_revision` and
`accept_intake_modulo_note_revision`; the accepted candidate pins its source
snapshot and explicitly reports `modulo_access_state=not_checked_by_noesis` and
`writeback_state=not_written`. It does not mutate the Modulo note. All cycle
observations are caller-reported; Iteration does not execute a changed
procedure or establish that a revision caused an outcome. Upstream gap routing,
the Modulo return/writeback journey, and live user assessment remain open under
[#1578](https://github.com/Ikey168/Noesis/issues/1578).

## Modulo handoff projection

`export_modulo_intake_handoff` returns [`noesis-modulo-intake-handoff-v1`](../../contracts/schemas/jsonschema/noesis-modulo-intake-handoff-v1.json) for the latest session revision. It includes the namespace and owner, a stable session-derived correlation key, the current mode/status/revision and timestamps, the prior session/reason for a transition, versioned Modulo return links, and versioned Noesis references with any source locators. An example for Awareness → Exploration is:

```json
{"contract":"noesis-modulo-intake-handoff-v1","scope":{"namespace":"research","owner":"alice"},"correlation_key":"intake:0123456789abcdef0123456789abcdef","session":{"id":"intake:0123456789abcdef0123456789abcdef","revision":1,"mode":"Exploration","status":"active","created_at_ms":1789000000000,"updated_at_ms":1789000000000},"transition":{"origin":{"session_id":"intake:abcdef0123456789abcdef0123456789","revision":2,"mode":"Awareness","reason":"Signal worth exploring"},"at_ms":1789000000000},"modulo_links":[{"system":"modulo","workspace_id":"home","kind":"intake_item","id":"item-1","version":1}],"noesis_references":[],"access_state":"current"}
```

Pass `contract_version="v2"` to request the additive [`noesis-modulo-intake-handoff-v2`](../../contracts/schemas/jsonschema/noesis-modulo-intake-handoff-v2.json) projection. The default remains v1 for existing clients. V2 includes the session intent, time budget, elapsed and remaining time as of `as_of_ms`, and unmet *recorded* completion checks. `validation_state="recorded_only"` means those checks do not certify real-world outcomes or satisfy additional typed-mode gates. Modulo can show these fields as progress while retaining Noesis as the authority for completion. Both versions recheck current session, owner, namespace, and linked-namespace access on every export.

The same envelope works for each mode. Modulo can link an Awareness item, Exploration save, Deep Research project, Decision Support choice, Problem-Solving task, Creation project, Externalization note, Internalization practice task, Iteration artifact, or Maintenance task using the matching `modulo_links.kind`; the `session.mode` identifies the Noesis workflow. Noesis owns acquired sources, evidence, claims, provenance, and authored artifacts. Modulo owns workspace organization, plans, and user-edited notes. A Modulo copy should be an explicit editable copy with its own identity; a linked projection should retain the Noesis ID/version. Re-export after corrections or revisions to refresh the projection. Deletion or access revocation must remove or hide the projection in Modulo; this export fails when current access to linked Noesis namespaces is missing. The envelope does not itself deliver changes to Modulo or verify its object IDs; the connector in Modulo #474 remains required.

## Recorded completion checks

| Mode | Session completion requires |
| --- | --- |
| Awareness | A bounded queue of feed item IDs, each explicitly assigned watch, escalate, schedule, discard, archive, or flag |
| Exploration | A recorded time-box end or escalation reason; an empty trail is valid |
| Deep Research | Versioned evidence-card, concept, claim-ledger, brief, mental-model, and map references; known/uncertain/unresolved text; Definition of Done review |
| Decision Support | Selected option, rationale, and a versioned decision reference |
| Problem-Solving | An explicit verified flag and verification description; typed trails require a latest passed observed check |
| Creation | A created-artifact reference and every declared acceptance check marked true |
| Externalization | A procedure reference and rehearsal or execution description |
| Internalization | At least one dated, recorded unaided demonstrated attempt with an answer |
| Iteration | Expected and observed outcomes, learning, and either a revised-artifact reference or a pinned local Modulo note candidate |
| Maintenance | Every checklist entry reviewed as done or deferred, with health marked acceptable |

These checks prevent accidental closure of an empty session. They do **not** establish that a cited source exists, that a fix works, or that a person has mastered a skill. Those claims require the corresponding Noesis subsystem receipt and real-world or human validation. An operator should leave a session active or paused when that evidence is unavailable.
Every returned session states `validation_state: recorded_only` until a native mode workflow can provide such receipts.

Deep Research permits three active or paused topics per owner by default across namespaces. Set `NOESIS_INTAKE_ACTIVE_RESEARCH_LIMIT` to an integer from 1 to 100 to change the limit. Awareness budgets are 1–15 minutes, Exploration 60–120 minutes, and Maintenance 30–60 minutes; their elapsed time is measured across pause/resume rather than inferred from a completed checkbox.

## Dedicated Modulo plugin identity and migration preview

`start_intake_mode` accepts optional `plugin_links` with separate
`workspace_id`, `account_id`, `plugin_id`, `collection`, `record_id`, and
`authoritative_version`. Each link declares `authority` and whether it is a
linked projection, intentional snapshot, or editable copy. An exact
`noesis_reference` and source locator can be attached where applicable. The
old `workspace_links` shape remains supported. A child session inherits its
parent's links and versioned references unless the caller supplies replacements;
the origin records the prior session revision and transition reason. Optional
`cadence={"interval_days": 1, "anchor_at_ms": 1789000000000}` records the
requested review interval without asserting that a scheduler was installed.

`export_modulo_intake_handoff(..., contract_version="v3")` adds the dedicated
plugin links, cadence, and stable transition ID. Its
`access_state="current_intake_namespace_only"` means Noesis rechecked its own owner,
namespace, and linked-namespace access. Its
`plugin_access_state="not_checked_by_noesis"` requires the Modulo connector to
recheck each plugin record/version before rendering or changing it. The v1
default and v2 progress projection remain available.

`recheck_modulo_plugin_link(namespace, session_id,
expected_session_revision, link_index)` rechecks one stored link through the
server-side `ModuloPluginLinkAccessStore`. A deployment may install a
`PerCallerModuloPluginLinkProvider` through
`configure_modulo_plugin_link_provider`; `for_caller(principal_id)` resolves a
read client from server-managed credentials. No credential is an MCP argument
or session field. The read sends the exact workspace, account, plugin,
collection, and record identity and returns only that identity, the pinned and
current versions, a check time, an access status, and one status: `current`,
`version_changed`, `revoked`, `missing`, or `unavailable`. Provider responses
with a mismatched identity or extra fields fail closed as `unavailable`; content
is never returned. With no configured provider the tool remains discoverable
but reports `unavailable`. It performs no Modulo write; signed-in round trips
remain live acceptance work.

The injected reader returns exactly `identity`, `status`, and
`authoritative_version`; `status` is `accessible`, `revoked`, `missing`, or
`unavailable`. It must distinguish a denied account/workspace grant from a
missing record and verify all five identity fields before reporting a version.
The existing flat plugin-state inventory callback does not expose account or
collection identity, so it cannot implement this exact recheck by itself. No
production provider is configured by default.

`discover_intake_workflows(namespace)` returns caller-scoped inputs, outputs,
blockers, local sessions, resource links, and allowed next commands. It does
not turn tool registration into a live-readiness claim.
`preview_modulo_intake_migration` accepts a bounded metadata-only inventory
from the dedicated plugins and returns exact link, mapping-review, or
original-plugin import actions; `inspect_modulo_intake_migration` pages the
immutable preview. The preview never writes Modulo records or deletes originals.
Its fixture or caller-supplied source label does not prove signed-in plugin
availability. Flashcard IDs, review logs, schedule parameters, attachments,
and relations must be checked against the authoritative Modulo records during
an actual migration.

## Current limits

The ledger and MCP tools implement the common session, handoff, history, access, and recorded-exit layer. Native inbox, Exploration, problem-trail, draft-playbook, and practice operations exist, along with an initial signed-in Modulo connector. Problem-Solving has a restricted action proposal/preview/consent/execution path for two action types; the public adapter registry is empty, and a configured integration plus live verification are still required. The roadmap still needs an authenticated newsletter delivery adapter, live quality evaluation of Exploration suggestions, complete Modulo workbenches, and a representative plugin-state migration. The token map gives the Knowledge Engine server's shared context a distinct caller identity when the token carries scopes. The full server still needs an authorization audit before exposure as a general multi-tenant Modulo gateway. The current daily-brief Blueprint node is a read-only input, not a triage workflow.

Reconciled with the issue checklists on 2026-09-24. The native Noesis
criteria of each mode are met and verified by deterministic tests, including
the artifact-level MCP journeys in
`tests/unit/tools/test_intake_native_journeys_mcp.py`. What remains is listed
below. It is predominantly cross-repository (signed-in Modulo), live-source, or
user-assessed evidence, and no mode issue is closed on deterministic evidence
alone.

| Issue | Native state | Remaining acceptance work |
| --- | --- | --- |
| [#1570](https://github.com/Ikey168/Noesis/issues/1570) Awareness | All native criteria met | Modulo same-record handoff; authenticated mailbox delivery; live daily-cadence evidence |
| [#1571](https://github.com/Ikey168/Noesis/issues/1571) Exploration | All native criteria met | Modulo device/restart workflow; suggestion quality with live use |
| [#1572](https://github.com/Ikey168/Noesis/issues/1572) Deep Research | All native criteria met | Modulo `ResearchProject` mapping and return artifacts; a representative live topic |
| [#1573](https://github.com/Ikey168/Noesis/issues/1573) Decision Support | All native criteria met | Modulo task/project return links; a real decision outcome |
| [#1574](https://github.com/Ikey168/Noesis/issues/1574) Problem-Solving | All native criteria met; the public adapter registry is empty | A configured integration; Modulo launch and Blueprint correlation; a live verified fix |
| [#1575](https://github.com/Ikey168/Noesis/issues/1575) Creation | All native criteria met; build adapters are deployment-configured | Modulo draft-authority and conflict workflow |
| [#1576](https://github.com/Ikey168/Noesis/issues/1576) Externalization | All native criteria met (kinds, automation, execution-backed trust) | Modulo Blueprint execution through the same connector |
| [#1577](https://github.com/Ikey168/Noesis/issues/1577) Internalization | All native criteria met; skills support independent assessment | Modulo practice-plugin journey; recorded human-assessed mastery |
| [#1578](https://github.com/Ikey168/Noesis/issues/1578) Iteration | All native criteria met | Signed-in Modulo cycles returning to the same plugin record; real feedback |
| [#1579](https://github.com/Ikey168/Noesis/issues/1579) Maintenance | All native criteria met; executed refresh/repair/archive/delete | Modulo dependents in impact previews; a live monthly review |
| [#1580](https://github.com/Ikey168/Noesis/issues/1580) Workflow foundations | All native criteria met | The versioned cross-repo mapping and authority contract proven with Modulo round trips |
| [#1581](https://github.com/Ikey168/Noesis/issues/1581) Migration | Preflight and flashcard import are native | Inventory, in-place linking and verification against installed Modulo plugins with user data |
| [#1582](https://github.com/Ikey168/Noesis/issues/1582) MCP foundations | All native criteria met | Publish the Modulo connector contract; render links validated by Modulo |
| [#1583](https://github.com/Ikey168/Noesis/issues/1583) Acceptance | Deterministic native journeys pass | Separate live/user evidence; cadence measurements; signed-in Modulo journeys |

The migration source is Modulo plugin state and existing browser-local intake records, as directed for [#1581](https://github.com/Ikey168/Noesis/issues/1581). Miniflux, Wallabag, Notion, and Anki exports are not part of this roadmap.
