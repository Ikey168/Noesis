# Information intake mode sessions

The `noesis-knowledge-engine` MCP server exposes a durable session ledger for the ten information workflow modes in [the intake roadmap](https://github.com/Ikey168/Noesis/issues/1569). It is a coordination layer over existing Noesis stores, not a second repository of sources, evidence, decisions, or reports. Modulo can retain its own planning and user-edited note state and store stable Noesis session and artifact references as specified in [Modulo #474](https://github.com/Ikey168/Modulo/issues/474).

The session response follows [`noesis-intake-session-v1`](../../contracts/schemas/jsonschema/noesis-intake-session-v1.json). The schema covers current and historical revisions, including redacted references after access revocation.

## Supported contract

Use `discover_intake_modes`, `route_intake_mode`, `start_intake_mode`, `inspect_intake_mode`, `list_intake_modes`, `command_intake_mode`, `export_intake_mode`, `verify_intake_mode_export`, and `export_modulo_intake_handoff` on the existing Knowledge Engine MCP server. Routing applies the ten intent questions in priority order and accepts an explicit user override. A session has an owner and namespace, a mode-specific time budget, a revision, optional origin session, versioned references, a command history, and a status. `command_intake_mode` supports `record`, `pause`, `resume`, `complete`, and `cancel`. It requires a fresh `expected_revision` and a stable `command_key`; replay of the same command returns the original revision, while reuse with a different action or payload fails. `start_intake_mode` likewise requires a stable `request_key`.

Readers need `knowledge:intake:read` and `namespace:<name>:read`; writers need `knowledge:intake:write` and `namespace:<name>:write`. The owner is checked on each non-operator call. For HTTP, configure `NOESIS_MCP_AUTH_TOKENS_FILE` as a private JSON object mapping bearer tokens to `{ "client_id": "user-id", "scopes": [...] }`; each token must be at least 32 characters and each client ID unique. Intake tools take the authenticated client ID and scopes from the request. Unauthenticated HTTP intake calls and the legacy shared token with no scopes cannot mutate sessions. Stdio retains the configured local principal and scopes. A reference to another namespace requires current read access to that namespace. Revoked references and derived session data are redacted on inspection, and completion/export fails while linked access is missing. An export includes every session revision and a SHA-256 digest for portable integrity checks; the digest alone does not authenticate the exporter.

The request identity is read through FastMCP's [access-token context](https://gofastmcp.com/v2/servers/context#access-tokens). The token file is a local integration option, not a substitute for a managed identity provider in a shared deployment.

For a transition, call `start_intake_mode` with an `origin` containing the previous `session_id` and a reason. The new session retains the prior mode and revision in its origin record. An optional `workspace_links` list carries versioned, opaque Modulo workspace/item/project/note/task IDs for return navigation. Do not copy source content into the session: attach a `{kind, id, namespace, version, locator?}` reference to the authoritative object. The session ledger does not validate that an arbitrary referenced object exists or a Modulo link is currently accessible, so callers must resolve IDs through the authoritative subsystem before relying on them.

## Native Awareness inbox (in progress)

The Knowledge Engine MCP server now exposes `subscribe_intake_feed`, `list_intake_feed_subscriptions`, `refresh_intake_feed_inbox`, `list_intake_feed_inbox`, `inspect_intake_feed_item`, `preview_intake_feed_signals`, `save_intake_feed_signal_rule`, `list_intake_feed_signal_rules`, `preview_intake_feed_signal_rule`, `mark_intake_feed_read`, `decide_intake_feed_item`, `annotate_intake_feed_item`, `start_awareness_from_inbox`, `triage_awareness_item`, `triage_awareness_batch`, and `promote_awareness_item`. A feed subscription is owned by one caller and namespace; its URL must be public, credential-free HTTPS. `refresh_intake_feed_inbox` additionally requires `knowledge:intake:fetch` and fetches at most 50 configured feeds with up to 100 entries each. It uses the existing RSS/Atom parser and a bounded network transport without implicit redirects or proxy credentials. A `newsletter_feed` is a newsletter that explicitly offers RSS/Atom; mailbox ingestion is not implemented.

The inbox preserves a source item's stable ID, original URL, publication time, content revision history, and annotations independently from read and triage state. Saved signal rules are owner-scoped and versioned; previews explain case-insensitive keyword matches without changing read or decision state. Refreshing the same item does not reset a decision. Listing or inspecting an item does not mark it read. `start_awareness_from_inbox` snapshots at most 1000 unprocessed items into a 1–15 minute session. `triage_awareness_item` and `triage_awareness_batch` commit decisions to both the inbox and session in one transaction; a command replay returns the original revision. `promote_awareness_item` requires an explicit escalate decision and carries exact source and annotation references into Exploration, Deep Research, or Problem-Solving. A Modulo caller can supply a versioned `workspace_links` entry for its already-persisted intake item, so the new session handoff points back to that item. Other Awareness work still uses the recorded-only session contract.

## Native Exploration capture (in progress)

An Exploration session can start with `start_intake_mode` and no research objective or output. `capture_exploration_page` records a visited or saved HTTPS URL, a user note, and optionally caller-supplied readable text. Set `fetch_readable=true` with `knowledge:intake:fetch` to acquire a public HTML or plain-text page through a bounded, no-proxy, no-redirect transport; extracted text records its acquisition method. Caller-supplied text remains labeled as such. Source snapshots keep stable IDs and historical versions, available through `inspect_exploration_source`. `annotate_exploration_source` attaches an immutable, idempotent note and optional locator to the exact current source version; historical inspection shows annotations for that version. A command replay does not refetch the page. `visit_exploration_feed_item` adds an Awareness source to the trail without recapturing it or changing its source identity. `suggest_exploration_sources` scans up to 500 of the owner's captured pages and returns explicit shared terms and two versioned source references; `lexical_overlap_v1` is a bounded keyword signal, not semantic or human-assessed serendipity. `decide_exploration_suggestion` durably dismisses or follows one suggestion; following adds a visit to the original source ID. A session can pause and resume through `command_intake_mode`; its completion still follows the recorded time box or an explicit escalation reason. Live quality acceptance remains to be implemented.

## Modulo handoff projection

`export_modulo_intake_handoff` returns [`noesis-modulo-intake-handoff-v1`](../../contracts/schemas/jsonschema/noesis-modulo-intake-handoff-v1.json) for the latest session revision. It includes the namespace and owner, a stable session-derived correlation key, the current mode/status/revision and timestamps, the prior session/reason for a transition, versioned Modulo return links, and versioned Noesis references with any source locators. An example for Awareness → Exploration is:

```json
{"contract":"noesis-modulo-intake-handoff-v1","scope":{"namespace":"research","owner":"alice"},"correlation_key":"intake:0123456789abcdef0123456789abcdef","session":{"id":"intake:0123456789abcdef0123456789abcdef","revision":1,"mode":"Exploration","status":"active","created_at_ms":1789000000000,"updated_at_ms":1789000000000},"transition":{"origin":{"session_id":"intake:abcdef0123456789abcdef0123456789","revision":2,"mode":"Awareness","reason":"Signal worth exploring"},"at_ms":1789000000000},"modulo_links":[{"system":"modulo","workspace_id":"home","kind":"intake_item","id":"item-1","version":1}],"noesis_references":[],"access_state":"current"}
```

The same envelope works for each mode. Modulo can link an Awareness item, Exploration save, Deep Research project, Decision Support choice, Problem-Solving task, Creation project, Externalization note, Internalization practice task, Iteration artifact, or Maintenance task using the matching `modulo_links.kind`; the `session.mode` identifies the Noesis workflow. Noesis owns acquired sources, evidence, claims, provenance, and authored artifacts. Modulo owns workspace organization, plans, and user-edited notes. A Modulo copy should be an explicit editable copy with its own identity; a linked projection should retain the Noesis ID/version. Re-export after corrections or revisions to refresh the projection. Deletion or access revocation must remove or hide the projection in Modulo; this export fails when current access to linked Noesis namespaces is missing. The envelope does not itself deliver changes to Modulo or verify its object IDs; the connector in Modulo #474 remains required.

## Recorded completion checks

| Mode | Session completion requires |
| --- | --- |
| Awareness | A bounded queue of feed item IDs, each explicitly assigned watch, escalate, schedule, discard, archive, or flag |
| Exploration | A recorded time-box end or escalation reason; an empty trail is valid |
| Deep Research | Versioned evidence-card, concept, claim-ledger, brief, mental-model, and map references; known/uncertain/unresolved text; Definition of Done review |
| Decision Support | Selected option, rationale, and a versioned decision reference |
| Problem-Solving | An explicit verified flag and verification description |
| Creation | A created-artifact reference and every declared acceptance check marked true |
| Externalization | A procedure reference and rehearsal or execution description |
| Internalization | At least one dated, recorded unaided demonstrated attempt with an answer |
| Iteration | Expected and observed outcomes, learning, and a revised-artifact reference |
| Maintenance | Every checklist entry reviewed as done or deferred, with health marked acceptable |

These checks prevent accidental closure of an empty session. They do **not** establish that a cited source exists, that a fix works, or that a person has mastered a skill. Those claims require the corresponding Noesis subsystem receipt and real-world or human validation. An operator should leave a session active or paused when that evidence is unavailable.
Every returned session states `validation_state: recorded_only` until a native mode workflow can provide such receipts.

Deep Research permits three active or paused topics per owner by default across namespaces. Set `NOESIS_INTAKE_ACTIVE_RESEARCH_LIMIT` to an integer from 1 to 100 to change the limit. Awareness budgets are 1–15 minutes, Exploration 60–120 minutes, and Maintenance 30–60 minutes; their elapsed time is measured across pause/resume rather than inferred from a completed checkbox.

## Current limits

The ledger and MCP tools implement the common session, handoff, history, access, and recorded-exit layer. The roadmap's remaining work includes direct newsletter inputs, provenance-backed Exploration suggestions, actual Research Flow composition, native playbook/practice engines, Modulo plugin-state migration, and the Modulo connector. The token map gives the Knowledge Engine server's shared context a distinct caller identity when the token carries scopes. The full server still needs an authorization audit before exposure as a general multi-tenant Modulo gateway. The current daily-brief Blueprint node is a read-only input, not a triage workflow.

Completion of [Noesis #1583](https://github.com/Ikey168/Noesis/issues/1583) still requires the full set of actual MCP transport journeys, representative live-source results, migration rehearsal, and user-assessed outcomes. The current stdio and HTTP tests cover session transport, replay, and isolation; they do not close any of the ten mode issues.

| Issue | Remaining acceptance work |
| --- | --- |
| [#1570](https://github.com/Ikey168/Noesis/issues/1570) Awareness | Add direct newsletter inputs, authorized Modulo plugin round trips, and live daily-cadence outcome evidence. |
| [#1571](https://github.com/Ikey168/Noesis/issues/1571) Exploration | Validate suggestion quality and Modulo device/restart workflow with representative live use. |
| [#1572](https://github.com/Ikey168/Noesis/issues/1572) Deep Research | Compose the existing project, acquisition, evidence, claim, brief, and map systems into a bounded run with a reviewed Definition of Done. |
| [#1573](https://github.com/Ikey168/Noesis/issues/1573) Decision Support | Connect the existing decision store to bounded intake evidence and user choice/return links. |
| [#1574](https://github.com/Ikey168/Noesis/issues/1574) Problem-Solving | Add a troubleshooting trail with authorized execution and observed verification receipts. |
| [#1575](https://github.com/Ikey168/Noesis/issues/1575) Creation | Connect authoring/build adapters, review, accepted output, and release authorization. |
| [#1576](https://github.com/Ikey168/Noesis/issues/1576) Externalization | Build native versioned playbooks and guided/executable procedure runs. |
| [#1577](https://github.com/Ikey168/Noesis/issues/1577) Internalization | Build native practice scheduling, unaided attempts, correction propagation, and Modulo practice-plugin state migration. |
| [#1578](https://github.com/Ikey168/Noesis/issues/1578) Iteration | Link observed outcomes and accepted revisions into authoritative artifact history. |
| [#1579](https://github.com/Ikey168/Noesis/issues/1579) Maintenance | Compose cross-mode staleness and failure queues with impact previews and recoverable actions. |
| [#1580](https://github.com/Ikey168/Noesis/issues/1580) Workflow foundations | Verify every handoff against authoritative objects and current access; add all required transition semantics. |
| [#1581](https://github.com/Ikey168/Noesis/issues/1581) Migration | Implement and rehearse migration from Modulo intake and practice plugins into durable plugin state and Noesis references; external app exports are out of scope. |
| [#1582](https://github.com/Ikey168/Noesis/issues/1582) MCP foundations | Add resource/prompt discovery, full preflight and service readiness, per-caller audit across all subsystems, and durable unattended delivery. |
| [#1583](https://github.com/Ikey168/Noesis/issues/1583) Acceptance | Run complete cross-mode journeys, live-source tests, real migration, and user-assessed outcomes. |

The migration source is Modulo plugin state and existing browser-local intake records, as directed for [#1581](https://github.com/Ikey168/Noesis/issues/1581). Miniflux, Wallabag, Notion, and Anki exports are not part of this roadmap.
